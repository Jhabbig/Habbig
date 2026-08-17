"""Pins ingest: all-errors-at-once, idempotency, sha256 short-circuit."""

from narve_sidecar import ingest as ing
from tests.conftest import add_question

PRED_CSV = (
    "source_id,question_id,predicted_probability,stated_at,note\n"
    "race_model,q-house,0.62,2026-08-01T12:00:00Z,first\n"
    "poll_agg,q-house,0.55,2026-08-01T13:00:00Z,\n"
)


def test_csv_predictions_ok_and_autocreate(conn):
    res = ing.ingest_file(conn, "predictions", PRED_CSV.encode(), "p.csv")
    assert res == {"ok_rows": 2, "err_rows": 0, "dedup_skipped": 0,
                   "already_ingested": False, "errors": []}
    q = conn.execute("SELECT title, status FROM questions WHERE id='q-house'").fetchone()
    assert q["title"] == "q-house" and q["status"] == "live"  # auto-created
    s = conn.execute("SELECT alpha, beta FROM sources WHERE id='race_model'").fetchone()
    assert (s["alpha"], s["beta"]) == (2.0, 2.0)  # auto-created neutral


def test_all_errors_at_once_with_1_based_lines(conn):
    csv_text = (
        "source_id,question_id,predicted_probability,stated_at,note\n"
        "a,q1,0.5,2026-08-01T12:00:00Z,ok\n"      # line 1 ok
        ",q1,0.5,2026-08-01T12:00:00Z,\n"          # line 2: missing source_id
        "a,q1,1.7,2026-08-01T12:00:00Z,\n"         # line 3: p out of range
        "a,q1,zzz,2026-08-01T12:00:00Z,\n"         # line 4: p not a number
        "a,q1,0.5,not-a-time,\n"                    # line 5: bad timestamp
    )
    res = ing.ingest_file(conn, "predictions", csv_text.encode(), "bad.csv")
    assert res["ok_rows"] == 1 and res["err_rows"] == 4
    assert [e["line"] for e in res["errors"]] == [2, 3, 4, 5]
    assert "source_id is required" in res["errors"][0]["reason"]
    assert "between 0 and 1" in res["errors"][1]["reason"]
    assert "must be a number" in res["errors"][2]["reason"]
    assert "ISO 8601" in res["errors"][3]["reason"]


def test_idempotent_reupload_dedup_skipped(conn):
    ing.ingest_file(conn, "predictions", PRED_CSV.encode(), "p.csv")
    grown = PRED_CSV + "race_model,q-house,0.64,2026-08-02T12:00:00Z,update\n"
    res = ing.ingest_file(conn, "predictions", grown.encode(), "p2.csv")
    assert res["ok_rows"] == 1 and res["dedup_skipped"] == 2
    assert res["already_ingested"] is False
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 3


def test_sha256_short_circuit(conn):
    first = ing.ingest_file(conn, "predictions", PRED_CSV.encode(), "p.csv")
    assert first["already_ingested"] is False
    again = ing.ingest_file(conn, "predictions", PRED_CSV.encode(), "renamed.csv")
    assert again == {"ok_rows": 2, "err_rows": 0, "dedup_skipped": 0,
                     "already_ingested": True, "errors": []}
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0] == 1


def test_bad_header_raises_ingest_error(conn):
    import pytest
    with pytest.raises(ing.IngestError, match="missing required column"):
        ing.ingest_file(conn, "predictions", b"foo,bar\n1,2\n", "x.csv")
    with pytest.raises(ing.IngestError, match="unknown column"):
        ing.ingest_file(
            conn, "resolutions",
            b"question_id,outcome,resolved_at,extra\nq,yes,2026-01-01T00:00:00Z,x\n",
            "x.csv")


def test_json_rows_path(conn):
    rows = [{"source_id": "a", "question_id": "q1",
             "predicted_probability": 0.61, "stated_at": "2026-08-01T12:00:00Z"}]
    res = ing.ingest_rows(conn, "predictions", rows)
    assert res["ok_rows"] == 1 and res["errors"] == []
    again = ing.ingest_rows(conn, "predictions", rows)
    assert again["already_ingested"] is True


def test_json_unknown_field_is_row_error(conn):
    rows = [{"source_id": "a", "question_id": "q1",
             "predicted_probability": 0.61, "stated_at": "2026-08-01T12:00:00Z",
             "surprise": 1}]
    res = ing.ingest_rows(conn, "predictions", rows)
    assert res["err_rows"] == 1
    assert "unknown field" in res["errors"][0]["reason"]


def test_markets_kind(conn):
    csv_text = (
        "venue,market_id,question_id,yes_price,liquidity,captured_at\n"
        "polymarket,m1,q-house,0.55,250000,2026-08-01T12:00:00Z\n"
        "kalshi,m2,q-house,0.57,,2026-08-01T12:00:00Z\n"
        "polymarket,m1,q-house,1.5,,2026-08-01T13:00:00Z\n"  # bad price
    )
    res = ing.ingest_file(conn, "markets", csv_text.encode(), "m.csv")
    assert res["ok_rows"] == 2 and res["err_rows"] == 1
    assert res["errors"][0]["line"] == 3
    row = conn.execute(
        "SELECT liquidity FROM market_snapshots WHERE venue='kalshi'").fetchone()
    assert row["liquidity"] is None
    # question auto-created so the snapshot is navigable
    assert conn.execute(
        "SELECT COUNT(*) FROM questions WHERE id='q-house'").fetchone()[0] == 1


def test_resolutions_kind_runs_the_loop(conn):
    ing.ingest_file(conn, "predictions", PRED_CSV.encode(), "p.csv")
    csv_text = (
        "question_id,outcome,resolved_at\n"
        "q-house,yes,2026-11-04T04:00:00Z\n"
        "q-house,no,2026-11-04T05:00:00Z\n"   # line 2: already resolved by line 1
        "nope,yes,2026-11-04T04:00:00Z\n"     # line 3: unknown question
    )
    res = ing.ingest_file(conn, "resolutions", csv_text.encode(), "r.csv")
    assert res["ok_rows"] == 1 and res["err_rows"] == 2
    reasons = {e["line"]: e["reason"] for e in res["errors"]}
    assert "already resolved" in reasons[2]
    assert "unknown question_id" in reasons[3]
    # both sources with a latest prediction on q-house got scored
    assert conn.execute(
        "SELECT COUNT(*) FROM credibility_events").fetchone()[0] == 2
    a = conn.execute("SELECT alpha FROM sources WHERE id='race_model'").fetchone()
    assert a["alpha"] == 3.0  # 0.62 >= 0.5 and outcome yes -> hit


def test_resolutions_void_row(conn):
    add_question(conn, "q-void")
    res = ing.ingest_rows(conn, "resolutions", [
        {"question_id": "q-void", "outcome": "void",
         "resolved_at": "2026-11-04T04:00:00Z"}])
    assert res["ok_rows"] == 1
    q = conn.execute("SELECT status FROM questions WHERE id='q-void'").fetchone()
    assert q["status"] == "void"


def test_utf8_binary_rejected_with_xlsx_hint(conn):
    import pytest
    with pytest.raises(ing.IngestError, match="XLSX is not supported"):
        ing.ingest_file(conn, "predictions", b"PK\x03\x04\xff\xfe\x00\x01", "x.xlsx")


def test_template_csv_headers():
    assert ing.template_csv("predictions").splitlines()[0] == \
        "source_id,question_id,predicted_probability,stated_at,note"
    assert ing.template_csv("markets").splitlines()[0] == \
        "venue,market_id,question_id,yes_price,liquidity,captured_at"
    assert ing.template_csv("resolutions").splitlines()[0] == \
        "question_id,outcome,resolved_at"
