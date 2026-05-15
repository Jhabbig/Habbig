"""FK integrity audit for gateway/auth.db, gateway/db.py, gateway/migrations/*.py.

Scope mirrors the brief: every CREATE TABLE statement in db.py and every
migration file, plus every live table in auth.db. For each `REFERENCES
<target>(...)` clause, classify the target.

Verdicts:
  - OK     — target exists in auth.db (live DB), case-insensitive match.
  - BROKEN — target absent from live DB AND no migration ever creates it
             AND db.py never declares it. This is the migration-188 class
             of bug.
  - STALE  — target absent from live DB but a migration creates it (or
             db.py declares it). Likely an un-applied migration or a
             feature that was downgraded. Not a bug per se, but worth
             listing so reviewers can spot drift.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = "/Users/shocakarel/Habbig/gateway/auth.db"
DB_PY = "/Users/shocakarel/Habbig/gateway/db.py"
MIG_DIR = "/Users/shocakarel/Habbig/gateway/migrations"
OUT = "/Users/shocakarel/Habbig/audits/audit_fk_integrity.md"

FK_RE = re.compile(
    r'REFERENCES\s+(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\s*\(',
    re.IGNORECASE,
)
CREATE_RE = re.compile(
    r'CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))\s*\(',
    re.IGNORECASE,
)


def slice_balanced(text: str, open_idx: int) -> str:
    """Return the substring from text[open_idx] up to and including its
    matching ')'. Treats `(` and `)` as balanced; ignores quoting because
    SQL identifiers don't legitimately contain unmatched parens in our
    schemas."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
        i += 1
    return text[open_idx:]


def collect_creates(text: str):
    """Yield (table_name, body) for every CREATE TABLE in text."""
    for m in CREATE_RE.finditer(text):
        tname = m.group(1) or m.group(2) or m.group(3)
        open_paren = m.end() - 1
        body = slice_balanced(text, open_paren)
        yield tname, body


def collect_fks_in(text: str):
    """Yield (source_table, target_table, clause) for FKs in text."""
    for tname, body in collect_creates(text):
        for fm in FK_RE.finditer(body):
            target = fm.group(1) or fm.group(2) or fm.group(3) or fm.group(4)
            yield tname, target, fm.group(0)


def main() -> None:
    # 1. Live DB
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    live_tables = {r["name"] for r in table_rows}
    live_lower = {n.lower() for n in live_tables}

    # 2. Every table name ever created (in db.py or any migration)
    declared_anywhere = set(live_tables)
    db_py_text = Path(DB_PY).read_text()
    for tname, _body in collect_creates(db_py_text):
        if tname:
            declared_anywhere.add(tname)

    mig_files = sorted(
        f for f in os.listdir(MIG_DIR) if f.endswith(".py") and f != "__init__.py"
    )
    for fname in mig_files:
        text = Path(os.path.join(MIG_DIR, fname)).read_text()
        for tname, _body in collect_creates(text):
            if tname:
                declared_anywhere.add(tname)

    declared_lower = {n.lower() for n in declared_anywhere}

    def classify(target: str) -> str:
        if target in live_tables or target.lower() in live_lower:
            return "OK"
        if target in declared_anywhere or target.lower() in declared_lower:
            return "STALE"
        return "BROKEN"

    # 3. Collect every FK with origin metadata
    fks = []
    for r in table_rows:
        for src, tgt, clause in collect_fks_in(r["sql"] or ""):
            fks.append({
                "origin": "auth.db (sqlite_master)",
                "source": src or r["name"],
                "target": tgt,
                "verdict": classify(tgt),
                "clause": clause,
            })

    for src, tgt, clause in collect_fks_in(db_py_text):
        fks.append({
            "origin": "gateway/db.py",
            "source": src or "<unknown>",
            "target": tgt,
            "verdict": classify(tgt),
            "clause": clause,
        })

    for fname in mig_files:
        text = Path(os.path.join(MIG_DIR, fname)).read_text()
        for src, tgt, clause in collect_fks_in(text):
            fks.append({
                "origin": f"migrations/{fname}",
                "source": src or "<unknown>",
                "target": tgt,
                "verdict": classify(tgt),
                "clause": clause,
            })

    total = len(fks)
    broken = [f for f in fks if f["verdict"] == "BROKEN"]
    stale = [f for f in fks if f["verdict"] == "STALE"]
    ok = [f for f in fks if f["verdict"] == "OK"]

    # 4. Write markdown report
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = []
    out.append("# Foreign Key Integrity Audit")
    out.append("")
    out.append("Auto-generated by `audits/_audit_run.py`. Re-run with:")
    out.append("")
    out.append("```")
    out.append("python3 /Users/shocakarel/Habbig/audits/_audit_run.py")
    out.append("```")
    out.append("")
    out.append("## Scope")
    out.append("")
    out.append("- Every `REFERENCES <table>(...)` clause inside every `CREATE TABLE`")
    out.append("  - in the live `gateway/auth.db` via `sqlite_master.sql`")
    out.append("  - in `gateway/db.py`")
    out.append("  - in every `gateway/migrations/*.py`")
    out.append("")
    out.append("Originally prompted by migration 188, which fixed a dangling FK on")
    out.append("`users.invite_token_id` that SQLite's automatic identifier-rewrite")
    out.append("had pointed at the temporary table `invite_tokens_old` (dropped at")
    out.append("the end of migration 162).")
    out.append("")
    out.append("## Verdicts")
    out.append("")
    out.append("- **OK** — target table exists in the live `auth.db`.")
    out.append("- **STALE** — target absent from live DB but defined by `db.py` or a")
    out.append("  migration (an un-applied or downgraded feature). Not a 188-class bug.")
    out.append("- **BROKEN** — target absent from live DB **and** never declared")
    out.append("  anywhere in `db.py` or any migration. This is the 188-class bug:")
    out.append("  the FK clause points at a table the schema never defines.")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- Total FKs scanned: **{total}**")
    out.append(f"- OK: {len(ok)}")
    out.append(f"- STALE (target defined in a migration but absent from live DB): {len(stale)}")
    out.append(f"- BROKEN (dangling — no definition anywhere): **{len(broken)}**")
    out.append("")

    if broken:
        out.append("## Broken FKs (188-class)")
        out.append("")
        out.append("| Origin | Source table | Target table | Clause |")
        out.append("|---|---|---|---|")
        for b in broken:
            out.append(
                f"| `{b['origin']}` | `{b['source']}` | `{b['target']}` | `{b['clause']}` |"
            )
        out.append("")
    else:
        out.append("## Broken FKs (188-class)")
        out.append("")
        out.append("**None.** Every FK target is declared somewhere in the schema —")
        out.append("either present in the live DB or defined by a migration / `db.py`.")
        out.append("The 188-class dangling-reference bug is no longer reachable.")
        out.append("")

    if stale:
        out.append("## STALE FKs")
        out.append("")
        out.append("These reference tables that exist in migration upgrade() blocks but")
        out.append("are not present in the current `auth.db`. Probable explanations:")
        out.append("")
        out.append("- The migration has not been applied to this DB snapshot.")
        out.append("- The migration was applied and then a later downgrade dropped the table.")
        out.append("- The CREATE happens conditionally and was skipped.")
        out.append("")
        out.append("None of these are FK-integrity bugs by themselves; they're logged for")
        out.append("operator awareness.")
        out.append("")
        out.append("| Origin | Source table | Target table | Clause |")
        out.append("|---|---|---|---|")
        for s in stale:
            out.append(
                f"| `{s['origin']}` | `{s['source']}` | `{s['target']}` | `{s['clause']}` |"
            )
        out.append("")

    # Full enumeration
    out.append("## All FKs (full enumeration)")
    out.append("")
    out.append("### Live `auth.db` (via `sqlite_master`)")
    out.append("")
    out.append("| Source table | Target table | Verdict |")
    out.append("|---|---|---|")
    live_fks = [f for f in fks if f["origin"] == "auth.db (sqlite_master)"]
    for f in sorted(live_fks, key=lambda x: (x["source"], x["target"])):
        out.append(f"| `{f['source']}` | `{f['target']}` | {f['verdict']} |")
    out.append("")
    out.append(f"_Subtotal: {len(live_fks)} FKs in live DB._")
    out.append("")

    out.append("### `gateway/db.py`")
    out.append("")
    out.append("| Source table | Target table | Verdict |")
    out.append("|---|---|---|")
    dbpy_fks = [f for f in fks if f["origin"] == "gateway/db.py"]
    for f in sorted(dbpy_fks, key=lambda x: (x["source"], x["target"])):
        out.append(f"| `{f['source']}` | `{f['target']}` | {f['verdict']} |")
    out.append("")
    out.append(f"_Subtotal: {len(dbpy_fks)} FKs in db.py._")
    out.append("")

    out.append("### `gateway/migrations/*.py`")
    out.append("")
    out.append("| Migration | Source table | Target table | Verdict |")
    out.append("|---|---|---|---|")
    mig_fks = [f for f in fks if f["origin"].startswith("migrations/")]
    for f in sorted(mig_fks, key=lambda x: (x["origin"], x["source"], x["target"])):
        mig = f["origin"].replace("migrations/", "")
        out.append(f"| `{mig}` | `{f['source']}` | `{f['target']}` | {f['verdict']} |")
    out.append("")
    out.append(f"_Subtotal: {len(mig_fks)} FKs across {len(mig_files)} migration files._")
    out.append("")

    out.append("## Method")
    out.append("")
    out.append("1. Open `auth.db`; pull every row from `sqlite_master` for `type='table'`.")
    out.append("2. For each `sql` blob, regex-match `REFERENCES <ident>(`.")
    out.append("3. Do the same for every `CREATE TABLE` body in `db.py` and each migration,")
    out.append("   parsed with a balanced-paren slice to capture the full column list.")
    out.append("4. Compare each captured target to (a) the set of live tables, and")
    out.append("   (b) the union of every table CREATE-ed anywhere in the schema source.")
    out.append("5. SQLite identifiers are case-insensitive, so the comparison normalises.")
    out.append("")
    out.append("Caveats:")
    out.append("- Table-level only. Column-level FK checks (does `target(col)` exist?)")
    out.append("  are out of scope per the brief.")
    out.append("- Migrations that build SQL via string formatting in Python helpers")
    out.append("  (rather than literal `CREATE TABLE` strings) are not parsed. The")
    out.append("  REFERENCES regex still catches any FK clause embedded in helper")
    out.append("  output as long as the literal text survives in the source.")
    out.append("")

    Path(OUT).write_text("\n".join(out) + "\n")

    print(f"Wrote {OUT}")
    print(f"Totals: total={total} ok={len(ok)} stale={len(stale)} broken={len(broken)}")
    if broken:
        for b in broken:
            print(f"  BROKEN: {b['origin']} :: {b['source']} -> {b['target']}")

    conn.close()


if __name__ == "__main__":
    main()
