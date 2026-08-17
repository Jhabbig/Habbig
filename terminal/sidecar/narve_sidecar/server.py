"""FastAPI sidecar server for narve Terminal v0.5.

Run: /opt/homebrew/bin/python3.11 -m narve_sidecar.server
Binds 127.0.0.1:41733. One SQLite DB (env NARVE_DB_PATH). CORS is limited to
the Tauri shell origins. Packaging seam: v1 ships this via pyinstaller or
briefcase; in dev the Tauri shell spawns it with tauri-plugin-shell.
"""

from __future__ import annotations

from contextlib import closing

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import __version__, credibility, sample_data
from . import db as db_mod
from . import ingest as ingest_mod

HOST = "127.0.0.1"
PORT = 41733
ALLOWED_ORIGINS = [
    "tauri://localhost",       # packaged app (macOS)
    "http://tauri.localhost",  # packaged app (Windows)
    "http://localhost:1420",   # tauri default devUrl
    "http://localhost:5173",   # vite dev (vite.config.ts pins 5173)
    "http://localhost:4173",   # vite preview
]


class ResolveBody(BaseModel):
    question_id: str
    outcome: str
    resolved_at: str | None = None


def _snapshot(row) -> dict:
    return {"venue": row["venue"], "market_id": row["market_id"],
            "yes_price": row["yes_price"], "liquidity": row["liquidity"],
            "captured_at": row["captured_at"]}


def _source_row(c, s) -> dict:
    n_resolved = c.execute(
        "SELECT COUNT(DISTINCT p.question_id) FROM predictions p"
        " JOIN questions q ON q.id = p.question_id"
        " WHERE p.source_id = ? AND q.status = 'resolved'", (s["id"],)
    ).fetchone()[0]
    n_live = c.execute(
        "SELECT COUNT(DISTINCT p.question_id) FROM predictions p"
        " JOIN questions q ON q.id = p.question_id"
        " WHERE p.source_id = ? AND q.status = 'live'", (s["id"],)
    ).fetchone()[0]
    last_active = c.execute(
        "SELECT MAX(stated_at) FROM predictions WHERE source_id = ?", (s["id"],)
    ).fetchone()[0]
    b = credibility.brier(c, s["id"])
    return {
        "id": s["id"], "name": s["name"], "kind": s["kind"],
        "alpha": s["alpha"], "beta": s["beta"],
        "credibility": round(credibility.credibility(s["alpha"], s["beta"]), 4),
        "n_resolved": n_resolved, "n_live": n_live,
        "brier": round(b, 4) if b is not None else None,
        "is_sample": bool(s["is_sample"]), "last_active": last_active,
    }


def _question_row(c, q) -> dict:
    n_sources = c.execute(
        "SELECT COUNT(DISTINCT source_id) FROM predictions WHERE question_id = ?",
        (q["id"],)).fetchone()[0]
    combined = credibility.consensus(c, q["id"])
    mkt = c.execute(
        "SELECT yes_price FROM market_snapshots WHERE question_id = ?"
        " ORDER BY captured_at DESC, id DESC LIMIT 1", (q["id"],)).fetchone()
    market_price = mkt["yes_price"] if mkt else None
    edge = (round(combined - market_price, 4)
            if combined is not None and market_price is not None else None)
    latest_pred = c.execute(
        "SELECT MAX(stated_at) FROM predictions WHERE question_id = ?",
        (q["id"],)).fetchone()[0]
    latest_snap = c.execute(
        "SELECT MAX(captured_at) FROM market_snapshots WHERE question_id = ?",
        (q["id"],)).fetchone()[0]
    candidates = [v for v in (q["created_at"], q["resolved_at"], latest_pred,
                              latest_snap) if v]
    return {
        "id": q["id"], "title": q["title"], "status": q["status"],
        "n_sources": n_sources,
        "combined_p": round(combined, 4) if combined is not None else None,
        "market_price": market_price, "edge": edge,
        "is_sample": bool(q["is_sample"]),
        "updated_at": max(candidates) if candidates else None,
    }


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or db_mod.db_path()
    app = FastAPI(title="narve sidecar", version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
        allow_methods=["*"], allow_headers=["*"],
    )
    with closing(db_mod.connect(path)):  # create db + run migrations up front
        pass

    def conn():
        return db_mod.connect(path)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "db": path}

    @app.post("/ingest/{kind}")
    async def ingest_endpoint(kind: str, request: Request):
        if kind not in ingest_mod.KINDS:
            raise HTTPException(404, f"unknown ingest kind {kind!r} — expected"
                                     " predictions, markets, resolutions or"
                                     " messages")
        content_type = (request.headers.get("content-type") or "").lower()
        with closing(conn()) as c:
            try:
                if content_type.startswith("multipart/"):
                    form = await request.form()
                    upload = form.get("file")
                    if upload is None or isinstance(upload, str):
                        raise HTTPException(
                            400, "multipart upload must include a 'file' part")
                    data = await upload.read()
                    return ingest_mod.ingest_file(c, kind, data, upload.filename)
                try:
                    body = await request.json()
                except Exception:
                    raise HTTPException(
                        400, "body must be multipart file or JSON {rows: [...]}")
                rows = body.get("rows") if isinstance(body, dict) else None
                if not isinstance(rows, list):
                    raise HTTPException(
                        400, "JSON body must be an object with a 'rows' array")
                return ingest_mod.ingest_rows(c, kind, rows)
            except ingest_mod.IngestError as exc:
                raise HTTPException(400, str(exc))

    @app.get("/templates/{kind}.csv")
    def template(kind: str):
        if kind not in ingest_mod.KINDS:
            raise HTTPException(404, f"unknown template kind {kind!r}")
        return PlainTextResponse(
            ingest_mod.template_csv(kind), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
        )

    @app.post("/sample/load")
    def sample_load():
        with closing(conn()) as c:
            counts = sample_data.load_sample(c)
        return {"loaded": True, "counts": counts}

    @app.get("/sources")
    def sources():
        with closing(conn()) as c:
            rows = [_source_row(c, s) for s in
                    c.execute("SELECT * FROM sources").fetchall()]
        # Credibility is a/(a+b) on INTEGER counts, so identical records tie
        # exactly (39 accounts at 5/2=0.714 in the 25k stress test). Within a
        # tie, rank by Brier (lower = sharper calls) — continuous, so it
        # separates skill from luck where hit-rate can't. No-Brier sources sink
        # to the bottom of their tie group; resolved-call count breaks the rest.
        rows.sort(key=lambda r: (
            -r["credibility"],
            r["brier"] if r["brier"] is not None else 2.0,
            -r["n_resolved"],
            r["id"],
        ))
        return rows

    @app.get("/sources/{source_id}")
    def source_detail(source_id: str):
        with closing(conn()) as c:
            s = c.execute("SELECT * FROM sources WHERE id = ?",
                          (source_id,)).fetchone()
            if s is None:
                raise HTTPException(404, f"unknown source_id {source_id!r}")
            out = _source_row(c, s)
            out["events"] = [dict(e) for e in c.execute(
                "SELECT * FROM credibility_events WHERE source_id = ?"
                " ORDER BY at DESC, id DESC", (source_id,)).fetchall()]
            out["predictions"] = [dict(p) for p in c.execute(
                "SELECT id, question_id, p, stated_at, note FROM predictions"
                " WHERE source_id = ? ORDER BY stated_at DESC, id DESC",
                (source_id,)).fetchall()]
        return out

    @app.get("/questions")
    def questions():
        with closing(conn()) as c:
            rows = [_question_row(c, q) for q in
                    c.execute("SELECT * FROM questions").fetchall()]
        rows.sort(key=lambda r: -1.0 if r["edge"] is None else abs(r["edge"]),
                  reverse=True)
        return rows

    @app.get("/questions/{question_id}")
    def question_detail(question_id: str):
        with closing(conn()) as c:
            q = c.execute("SELECT * FROM questions WHERE id = ?",
                          (question_id,)).fetchone()
            if q is None:
                raise HTTPException(404, f"unknown question_id {question_id!r}")
            per_source = []
            for pred in credibility.latest_predictions(c, question_id):
                s = c.execute("SELECT alpha, beta FROM sources WHERE id = ?",
                              (pred["source_id"],)).fetchone()
                cred = (round(credibility.credibility(s["alpha"], s["beta"]), 4)
                        if s else None)
                per_source.append({"source_id": pred["source_id"],
                                   "credibility": cred, "p": pred["p"],
                                   "stated_at": pred["stated_at"]})
            per_source.sort(key=lambda r: r["credibility"] or 0.0, reverse=True)
            combined = credibility.consensus(c, question_id)
            market = [_snapshot(r) for r in c.execute(
                """SELECT * FROM (
                       SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY venue ORDER BY captured_at DESC, id DESC
                       ) AS rn FROM market_snapshots WHERE question_id = ?
                   ) WHERE rn = 1 ORDER BY venue""", (question_id,)).fetchall()]
            history = [_snapshot(r) for r in c.execute(
                "SELECT * FROM market_snapshots WHERE question_id = ?"
                " ORDER BY captured_at ASC, id ASC", (question_id,)).fetchall()]
            messages = [dict(m) for m in c.execute(
                "SELECT id, source_id, text, sent_at, stance_p FROM messages"
                " WHERE question_id = ? ORDER BY sent_at DESC, id DESC",
                (question_id,)).fetchall()]
            question = {"id": q["id"], "title": q["title"], "status": q["status"],
                        "resolved_outcome": q["resolved_outcome"],
                        "resolved_at": q["resolved_at"],
                        "is_sample": bool(q["is_sample"]),
                        "created_at": q["created_at"]}
        return {"question": question, "per_source": per_source,
                "combined_p": round(combined, 4) if combined is not None else None,
                "market": market, "history": history, "messages": messages}

    @app.post("/resolve")
    def resolve(body: ResolveBody):
        with closing(conn()) as c:
            try:
                moves = credibility.resolve_question(
                    c, body.question_id, body.outcome, body.resolved_at)
            except credibility.QuestionNotFound:
                raise HTTPException(404, f"unknown question_id {body.question_id!r}")
            except credibility.AlreadyResolved:
                raise HTTPException(
                    409, f"question {body.question_id!r} is already resolved")
            except credibility.InvalidOutcome as exc:
                raise HTTPException(422, str(exc))
        return {"resolved": True, "moves": moves}

    @app.get("/raw/{kind}")
    def raw(kind: str, question_id: str | None = None,
            source_id: str | None = None, limit: int = 200, offset: int = 0):
        if kind not in ingest_mod.KINDS:
            raise HTTPException(404, f"unknown raw kind {kind!r}")
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        where, args = [], []
        if kind == "predictions":
            base, order = "FROM predictions", "ORDER BY stated_at DESC, id DESC"
            if question_id:
                where.append("question_id = ?"); args.append(question_id)
            if source_id:
                where.append("source_id = ?"); args.append(source_id)
            select = "SELECT id, source_id, question_id, p, stated_at, note "
        elif kind == "markets":
            base, order = "FROM market_snapshots", "ORDER BY captured_at DESC, id DESC"
            if question_id:
                where.append("question_id = ?"); args.append(question_id)
            select = ("SELECT id, venue, market_id, question_id, yes_price,"
                      " liquidity, captured_at ")
        elif kind == "messages":
            base, order = "FROM messages", "ORDER BY sent_at DESC, id DESC"
            if question_id:
                where.append("question_id = ?"); args.append(question_id)
            if source_id:
                where.append("source_id = ?"); args.append(source_id)
            select = ("SELECT id, source_id, question_id, text, sent_at,"
                      " stance_p ")
        else:  # resolutions live ON questions in v0.5
            base, order = "FROM questions", "ORDER BY resolved_at DESC, id DESC"
            where.append("status != 'live'")
            if question_id:
                where.append("id = ?"); args.append(question_id)
            select = ("SELECT id AS question_id, CASE WHEN status = 'void' THEN"
                      " 'void' WHEN resolved_outcome = 1 THEN 'yes' ELSE 'no'"
                      " END AS outcome, resolved_at ")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with closing(conn()) as c:
            total = c.execute(f"SELECT COUNT(*) {base}{clause}", args).fetchone()[0]
            rows = [dict(r) for r in c.execute(
                f"{select}{base}{clause} {order} LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()]
        return {"rows": rows, "total": total}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
