"""Pins people-as-sources: messages ingest, stance -> prediction bridge,
texter credibility via the same Beta(2,2) resolve loop, sample data v2."""

from narve_sidecar import credibility as cr
from narve_sidecar import ingest as ing

MSG_CSV = (
    "source_id,text,sent_at,question_id,stance\n"
    "staffer,Whip count says the majority holds,2026-08-01T12:00:00Z,q-house,0.7\n"
    "staffer,Mood on the floor is easy,2026-08-01T13:00:00Z,q-house,\n"
)


def test_stance_validation_row_errors(conn):
    rows = [
        {"source_id": "s", "text": "t1", "sent_at": "2026-08-01T12:00:00Z",
         "question_id": "q1", "stance": 1.2},          # line 1: out of range
        {"source_id": "s", "text": "t2", "sent_at": "2026-08-01T12:00:00Z",
         "question_id": "q1", "stance": "yes"},        # line 2: never a number
        {"source_id": "s", "text": "t3", "sent_at": "2026-08-01T12:00:00Z"},
    ]
    res = ing.ingest_rows(conn, "messages", rows)
    assert res["ok_rows"] == 1 and res["err_rows"] == 2
    reasons = {e["line"]: e["reason"] for e in res["errors"]}
    assert "between 0 and 1" in reasons[1]
    assert "must be a number" in reasons[2]  # yes/no words are hard errors
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_texter_enters_credibility_loop(conn):
    res = ing.ingest_rows(conn, "messages", [
        {"source_id": "staffer", "text": "Whip count says it holds",
         "sent_at": "2026-08-01T12:00:00Z", "question_id": "q-house",
         "stance": 0.7}])
    assert res["ok_rows"] == 1 and res["errors"] == []
    m = conn.execute("SELECT * FROM messages").fetchone()
    assert m["stance_p"] == 0.7 and m["question_id"] == "q-house"
    assert m["text_hash"] == ing.text_hash("Whip count says it holds")
    p = conn.execute("SELECT * FROM predictions").fetchone()  # BOTH rows exist
    assert p["source_id"] == "staffer" and p["p"] == 0.7
    assert p["stated_at"] == "2026-08-01T12:00:00Z"
    assert p["note"] == "Whip count says it holds"
    q = conn.execute(
        "SELECT title, status FROM questions WHERE id='q-house'").fetchone()
    assert q["title"] == "q-house" and q["status"] == "live"  # auto-created
    moves = cr.resolve_question(conn, "q-house", "yes")
    assert moves == [{"source_id": "staffer", "old_cred": 0.5,
                      "new_cred": 0.6, "hit": True}]  # 2/2 -> hit -> 3/5
    s = conn.execute(
        "SELECT alpha, beta, kind FROM sources WHERE id='staffer'").fetchone()
    assert (s["alpha"], s["beta"]) == (3.0, 2.0) and s["kind"] == "user"


def test_long_text_prediction_note_truncated_100(conn):
    text = "x" * 150
    ing.ingest_rows(conn, "messages", [
        {"source_id": "s", "text": text, "sent_at": "2026-08-01T12:00:00Z",
         "question_id": "q1", "stance": 0.9}])
    assert conn.execute("SELECT note FROM predictions").fetchone()["note"] == "x" * 100
    assert conn.execute("SELECT text FROM messages").fetchone()["text"] == text


def test_context_only_and_unlinked_stance_create_no_prediction(conn):
    res = ing.ingest_rows(conn, "messages", [
        {"source_id": "s", "text": "context only",
         "sent_at": "2026-08-01T12:00:00Z", "question_id": "q1"},
        {"source_id": "s", "text": "stance, no question",
         "sent_at": "2026-08-01T13:00:00Z", "stance": 0.8}])
    assert res["ok_rows"] == 2
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    rows = conn.execute(
        "SELECT question_id, stance_p FROM messages ORDER BY sent_at").fetchall()
    assert (rows[0]["question_id"], rows[0]["stance_p"]) == ("q1", None)
    assert (rows[1]["question_id"], rows[1]["stance_p"]) == (None, 0.8)


def test_dedupe_on_reupload(conn):
    ing.ingest_file(conn, "messages", MSG_CSV.encode(), "m.csv")
    grown = MSG_CSV + "staffer,A new development,2026-08-01T14:00:00Z,,\n"
    res = ing.ingest_file(conn, "messages", grown.encode(), "m2.csv")
    assert res["ok_rows"] == 1 and res["dedup_skipped"] == 2
    assert res["already_ingested"] is False
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    again = ing.ingest_file(conn, "messages", grown.encode(), "renamed.csv")
    assert again["already_ingested"] is True  # sha256 short-circuit
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


def test_autocreated_sources_have_kind_user(conn):
    ing.ingest_rows(conn, "messages", [
        {"source_id": "texter", "text": "hi",
         "sent_at": "2026-08-01T12:00:00Z"}])
    s = conn.execute(
        "SELECT kind, alpha, beta FROM sources WHERE id='texter'").fetchone()
    assert s["kind"] == "user" and (s["alpha"], s["beta"]) == (2.0, 2.0)
    ing.ingest_rows(conn, "predictions", [
        {"source_id": "pred_src", "question_id": "q1",
         "predicted_probability": 0.5, "stated_at": "2026-08-01T12:00:00Z"}])
    row = conn.execute("SELECT kind FROM sources WHERE id='pred_src'").fetchone()
    assert row["kind"] == "user"  # predictions auto-create is 'user' too


def test_question_detail_includes_messages_newest_first(client):
    res = client.post("/ingest/messages", json={"rows": [
        {"source_id": "s", "text": "older", "sent_at": "2026-08-01T12:00:00Z",
         "question_id": "q1", "stance": 0.7},
        {"source_id": "s", "text": "newer", "sent_at": "2026-08-02T12:00:00Z",
         "question_id": "q1"},
        {"source_id": "s", "text": "unlinked",
         "sent_at": "2026-08-03T12:00:00Z"}]})
    assert res.json()["ok_rows"] == 3
    msgs = client.get("/questions/q1").json()["messages"]
    assert [m["text"] for m in msgs] == ["newer", "older"]  # newest first
    assert set(msgs[0]) == {"id", "source_id", "text", "sent_at", "stance_p"}
    assert msgs[0]["stance_p"] is None and msgs[1]["stance_p"] == 0.7


def test_raw_messages_endpoint(client):
    client.post("/ingest/messages", json={"rows": [
        {"source_id": "a", "text": "one", "sent_at": "2026-08-01T12:00:00Z",
         "question_id": "q1", "stance": 0.6},
        {"source_id": "b", "text": "two", "sent_at": "2026-08-02T12:00:00Z"}]})
    body = client.get("/raw/messages").json()
    assert set(body) == {"rows", "total"} and body["total"] == 2
    assert [r["text"] for r in body["rows"]] == ["two", "one"]  # newest first
    assert set(body["rows"][0]) == {"id", "source_id", "question_id", "text",
                                    "sent_at", "stance_p"}
    filtered = client.get("/raw/messages", params={"source_id": "a"}).json()
    assert filtered["total"] == 1 and filtered["rows"][0]["stance_p"] == 0.6
    by_q = client.get("/raw/messages", params={"question_id": "q1"}).json()
    assert by_q["total"] == 1


def test_template_messages_csv(client):
    res = client.get("/templates/messages.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == "source_id,text,sent_at,question_id,stance"
    assert ing.template_csv("messages").splitlines()[0] == \
        "source_id,text,sent_at,question_id,stance"


def test_sample_load_adds_capitol_staffer_and_stays_idempotent(client):
    first = client.post("/sample/load").json()
    srcs = {r["id"]: r for r in client.get("/sources").json()}
    staff = srcs["sample:capitol_staffer"]
    assert staff["kind"] == "user"
    assert staff["alpha"] == 2.0 and staff["beta"] == 2.0  # fresh 2/2 prior
    assert all(r["kind"] == "model" for rid, r in srcs.items()
               if rid != "sample:capitol_staffer")
    raw = client.get("/raw/messages",
                     params={"source_id": "sample:capitol_staffer"}).json()
    assert raw["total"] == 3
    stances = sorted(r["stance_p"] for r in raw["rows"]
                     if r["stance_p"] is not None)
    assert stances == [0.66, 0.7]
    preds = client.get("/raw/predictions",
                       params={"source_id": "sample:capitol_staffer"}).json()
    # the question+stance message landed one + the v3 DJT sample prediction
    assert preds["total"] == 2
    by_q = {r["question_id"]: r["p"] for r in preds["rows"]}
    assert by_q == {"midterm-house-gop-2026": 0.66,
                    "trump-2026-rally-tour": 0.7}
    drill = client.get("/questions/midterm-house-gop-2026").json()
    assert [m["stance_p"] for m in drill["messages"]] == [None, 0.66]
    again = client.post("/sample/load").json()
    assert again["counts"] == first["counts"]  # second load is a no-op
    assert client.get("/raw/messages").json()["total"] == 3


def test_failed_ingest_is_never_sealed_by_its_sha(conn):
    """A file whose first ingest had row errors must fully reprocess on
    retry (same bytes), reporting the errors again — not replay logged counts
    with an empty errors list. Clean ingests still short-circuit."""
    from narve_sidecar import ingest

    bad = (b"source_id,text,sent_at,question_id,stance\n"
           b"u1,call it,2026-01-01T00:00:00Z,q1,1.4\n"       # stance out of range
           b"u1,context only,2026-01-01T00:01:00Z,q1,\n")
    r1 = ingest.ingest_file(conn, "messages", bad, "mixed.csv")
    assert (r1["ok_rows"], r1["err_rows"], r1["already_ingested"]) == (1, 1, False)

    r2 = ingest.ingest_file(conn, "messages", bad, "mixed.csv")
    assert r2["already_ingested"] is False          # NOT sealed
    assert r2["err_rows"] == 1 and r2["errors"]     # errors visible again
    assert r2["dedup_skipped"] == 1                 # the good row deduped, not duplicated

    good = (b"source_id,text,sent_at,question_id,stance\n"
            b"u2,clean call,2026-01-02T00:00:00Z,q2,0.7\n")
    ingest.ingest_file(conn, "messages", good, "good.csv")
    r3 = ingest.ingest_file(conn, "messages", good, "good.csv")
    assert r3["already_ingested"] is True           # clean files still short-circuit
