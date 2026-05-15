"""Guard against migrations re-introducing dangling FK references.

See Obsidian note `narve SQLite FK Auto-Rewrite Bug` and migration 197
(`197_fix_sessions_users_fk.py`) for the original incident: an
`ALTER TABLE ... RENAME` left ``REFERENCES users_old(id)`` baked into
the `sessions` schema, and SQLite happily kept the dangling FK without
complaining at write time. Later migrations that tried to drop or
recreate the parent table couldn't, and `PRAGMA foreign_key_check`
silently returned rows pointing at a vanished parent.

This test fails CI whenever a migration leaves behind:

1. A FK that still references a known scratch / temp-table suffix
   (``_old``, ``_drop_``, ``_fk_fix``, ``_backup``, ``_tmp``, ``_new``).
2. A FK whose referenced table does not exist in ``sqlite_master``.
3. Any row in ``PRAGMA foreign_key_check``.
4. A non-``ok`` result from ``PRAGMA integrity_check``.

The test runs against the same in-memory DB the rest of the new-feature
suite uses (``tests/_testdb.py`` applies every migration through
``migrations.upgrade_to_head`` at import time), so coverage moves in
lockstep with the migration list.
"""

from __future__ import annotations

import re
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB w/ migrations

import db  # noqa: E402


USES_TESTDB = True


# Suffixes that mark scratch / rebuild tables created during a
# `RENAME TABLE` migration step. If any of these appear inside a FK
# clause in a live `sqlite_master.sql` row, a migration forgot to clean
# itself up and the parent table is dangling.
_TEMP_SUFFIXES: tuple[str, ...] = (
    "_old",
    "_drop_",
    "_fk_fix",
    "_backup",
    "_tmp",
    "_new",
)


# Matches:  REFERENCES "tbl"(col)   |  REFERENCES tbl (col)  |
#           REFERENCES [tbl](col)   |  REFERENCES `tbl`(col)
_FK_REF_RE = re.compile(
    r'REFERENCES\s+["\'`\[]?([A-Za-z_][A-Za-z0-9_]*)["\'`\]]?\s*\(',
    re.IGNORECASE,
)


def _live_tables(c) -> set[str]:
    rows = c.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def _schema_rows(c) -> list[tuple[str, str, str]]:
    """Return ``(type, name, sql)`` for every non-internal entry."""
    rows = c.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [(r["type"], r["name"], r["sql"]) for r in rows]


class NoDanglingForeignKeysTests(unittest.TestCase):
    """Migration hygiene: no dangling FK targets, no temp-table leftovers."""

    def test_no_temp_table_suffixes_in_schema(self):
        """No live schema entry should reference a scratch / temp table."""
        with db.conn() as c:
            offenders: list[tuple[str, str, str]] = []
            for kind, name, sql in _schema_rows(c):
                lowered = sql.lower()
                for sfx in _TEMP_SUFFIXES:
                    # Quick substring filter — the regex below proves the
                    # match is inside a FK REFERENCES clause and not, say,
                    # a column name like `updated_old_ts` happening to
                    # contain "_old".
                    if sfx not in lowered:
                        continue
                    for target in _FK_REF_RE.findall(sql):
                        if target.lower().endswith(sfx):
                            offenders.append((kind, name, target))
            self.assertEqual(
                offenders, [],
                "Found FK references to temp / scratch tables — a "
                "migration left a dangling reference behind. Each entry "
                "is (object_type, object_name, target_table):\n"
                + "\n".join(f"  - {row}" for row in offenders),
            )

    def test_every_fk_target_table_exists(self):
        """``REFERENCES <name>(...)`` must point at a real, live table."""
        with db.conn() as c:
            live = _live_tables(c)
            dangling: list[tuple[str, str, str]] = []
            for kind, name, sql in _schema_rows(c):
                for target in _FK_REF_RE.findall(sql):
                    if target not in live:
                        dangling.append((kind, name, target))
            self.assertEqual(
                dangling, [],
                "FK references to tables that do not exist in "
                "sqlite_master — a migration dropped the parent without "
                "fixing dependents. Each entry is "
                "(object_type, object_name, missing_target):\n"
                + "\n".join(f"  - {row}" for row in dangling),
            )

    def test_pragma_foreign_key_check_is_clean(self):
        """``PRAGMA foreign_key_check`` must return zero rows."""
        with db.conn() as c:
            rows = c.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(
                [tuple(r) for r in rows], [],
                "PRAGMA foreign_key_check returned violations — a "
                "migration left orphan rows or a broken parent ref:\n"
                + "\n".join(f"  - {tuple(r)}" for r in rows),
            )

    def test_pragma_integrity_check_is_ok(self):
        """``PRAGMA integrity_check`` must return exactly ``("ok",)``."""
        with db.conn() as c:
            rows = c.execute("PRAGMA integrity_check").fetchall()
            tup = tuple(rows[0]) if rows else ()
            self.assertEqual(
                tup, ("ok",),
                f"PRAGMA integrity_check failed: {[tuple(r) for r in rows]!r}",
            )


if __name__ == "__main__":
    unittest.main()
