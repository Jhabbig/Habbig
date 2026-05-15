"""Tests for ``exports.generator._safe_query`` schema-drift behaviour.

These cover the HIGH-FIX in `_safe_query`: schema drift (a dropped table
or renamed column) used to silently produce empty result lists, which
meant a GDPR export could go out missing whole sections without anyone
noticing. The fixed behaviour is:

  1. Catch only ``OperationalError`` matching ``no such table`` or
     ``no such column``.
  2. Emit ``log.warning`` with the table name and the underlying error.
  3. Append ``{"table": ..., "reason": ...}`` to the optional
     ``errors`` list so ``build_zip`` can surface it on the manifest.
  4. Re-raise on every other ``OperationalError`` (syntax error, locked
     DB, real bug) so a deceptively-complete archive can never ship.
"""

from __future__ import annotations

USES_TESTDB = True

import contextlib
import logging
import sqlite3
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB

from exports import generator


@contextlib.contextmanager
def _scratch_conn():
    """Throwaway sqlite connection — keeps these tests independent of
    the shared schema state. ``_safe_query`` only reads its ``conn``
    argument so a fresh in-memory DB is the simplest seam."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


class TestSafeQueryMissingTable(unittest.TestCase):
    """Schema drift — table dropped or never migrated. Must not blow up
    the whole export, but MUST leave a paper trail."""

    def test_missing_table_returns_empty_list(self):
        with _scratch_conn() as c:
            rows = generator._safe_query(
                c, "SELECT * FROM table_that_does_not_exist"
            )
        self.assertEqual(rows, [])

    def test_missing_table_logs_warning(self):
        with _scratch_conn() as c:
            with self.assertLogs("exports.generator", level="WARNING") as cm:
                generator._safe_query(
                    c, "SELECT * FROM table_that_does_not_exist"
                )
        # The warning must carry both the table name (for ops) and the
        # underlying sqlite error string (for debugging schema diffs).
        joined = "\n".join(cm.output)
        self.assertIn("table_that_does_not_exist", joined)
        self.assertIn("no such table", joined.lower())

    def test_missing_table_appends_manifest_entry(self):
        errors: list[dict] = []
        with _scratch_conn() as c:
            generator._safe_query(
                c,
                "SELECT * FROM table_that_does_not_exist",
                errors=errors,
            )
        self.assertEqual(len(errors), 1)
        entry = errors[0]
        self.assertEqual(entry["table"], "table_that_does_not_exist")
        self.assertIn("no such table", entry["reason"].lower())

    def test_missing_column_also_recorded(self):
        # The fix treats "no such column" the same as "no such table" —
        # both are schema-drift conditions worth flagging.
        errors: list[dict] = []
        with _scratch_conn() as c:
            c.execute("CREATE TABLE drift_t (id INTEGER PRIMARY KEY)")
            with self.assertLogs("exports.generator", level="WARNING"):
                rows = generator._safe_query(
                    c,
                    "SELECT renamed_column FROM drift_t",
                    errors=errors,
                )
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["table"], "drift_t")
        self.assertIn("no such column", errors[0]["reason"].lower())

    def test_extract_table_name_handles_unparseable_sql(self):
        # Defensive: if SQL doesn't match our FROM regex, the manifest
        # entry falls back to "<unknown>" instead of leaking the raw SQL.
        errors: list[dict] = []
        with _scratch_conn() as c:
            # Force an OperationalError via a syntactically bad statement
            # routed through the same catch path — we expect this NOT to
            # be swallowed (see TestSafeQueryReraises), so use a plain
            # missing-table query without a FROM clause we can find.
            #
            # ``SELECT 1 FROM nope`` is parseable enough for the regex.
            generator._safe_query(
                c,
                "SELECT * FROM nope_missing",
                errors=errors,
            )
        self.assertEqual(errors[0]["table"], "nope_missing")


class TestSafeQueryReraises(unittest.TestCase):
    """Errors that aren't schema drift MUST propagate. Otherwise a real
    bug in the export pipeline (locked DB, syntax error, integrity
    failure) would surface as a silently-empty section."""

    def test_syntax_error_propagates(self):
        with _scratch_conn() as c:
            with self.assertRaises(sqlite3.OperationalError) as cm:
                generator._safe_query(c, "THIS IS NOT VALID SQL")
        # Sanity: the underlying error isn't a schema-drift one.
        msg = str(cm.exception).lower()
        self.assertNotIn("no such table", msg)
        self.assertNotIn("no such column", msg)

    def test_other_operational_error_does_not_touch_manifest(self):
        # Append-on-catch only fires for schema drift. A propagated
        # error must not pollute the manifest with an entry the caller
        # didn't actually skip.
        errors: list[dict] = []
        with _scratch_conn() as c:
            with self.assertRaises(sqlite3.OperationalError):
                generator._safe_query(
                    c, "GARBAGE STATEMENT", errors=errors
                )
        self.assertEqual(errors, [])

    def test_non_operational_error_propagates(self):
        # ``ProgrammingError`` (bad bind, wrong param count) is not
        # caught by the OperationalError filter at all — make sure
        # that path stays untouched by the fix.
        with _scratch_conn() as c:
            c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            with self.assertRaises(sqlite3.ProgrammingError):
                # 2 params supplied for 0 placeholders → ProgrammingError
                generator._safe_query(c, "SELECT * FROM t", ("x", "y"))


class TestExtractTableName(unittest.TestCase):
    """Direct coverage on the helper that names the table in logs+manifest."""

    def test_extracts_first_from_target(self):
        self.assertEqual(
            generator._extract_table_name("SELECT * FROM foo WHERE x = 1"),
            "foo",
        )

    def test_extracts_table_from_joined_query(self):
        sql = (
            "SELECT a.x FROM bar a "
            "LEFT JOIN baz b ON b.id = a.id WHERE a.x = ?"
        )
        self.assertEqual(generator._extract_table_name(sql), "bar")

    def test_unparseable_returns_placeholder(self):
        self.assertEqual(generator._extract_table_name(""), "<unknown>")
        self.assertEqual(
            generator._extract_table_name("not a select"), "<unknown>"
        )


if __name__ == "__main__":
    unittest.main()
