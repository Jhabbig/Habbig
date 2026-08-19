"""Pins account context: accounts ingest UPSERT, deterministic token-overlap
relevance (context_match / relevant_sources), the DJT rule (bias/region
spreads + skew_note), and sample data v3."""

from narve_sidecar import context as ctx
from narve_sidecar import ingest as ing
from tests.conftest import add_prediction, add_question, add_source


def _src(conn, sid):
    return conn.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()


def _pred_src(conn, qid, sid, bias="unknown", region=""):
    """A source with context that already PREDICTED on qid."""
    conn.execute(
        "INSERT INTO sources(id, bias, region, created_at)"
        " VALUES (?, ?, ?, '2026-01-01T00:00:00Z')", (sid, bias, region))
    conn.execute(
        "INSERT INTO predictions(source_id, question_id, p, stated_at)"
        " VALUES (?, ?, 0.6, '2026-01-01T00:00:00Z')", (sid, qid))
    conn.commit()


# --- accounts ingest: UPSERT semantics ---------------------------------------

def test_accounts_autocreate_unknown_source_neutral_user(conn):
    res = ing.ingest_rows(conn, "accounts", [
        {"source_id": "anon_trader", "bias": "right", "topics": "fed rates",
         "followers": 5200, "verified": True, "notes": "sharp on macro"}])
    assert res == {"ok_rows": 1, "err_rows": 0, "dedup_skipped": 0,
                   "already_ingested": False, "errors": []}
    s = _src(conn, "anon_trader")
    assert (s["alpha"], s["beta"], s["kind"]) == (2.0, 2.0, "user")
    assert s["bias"] == "right" and s["topics"] == "fed rates"
    assert s["followers"] == 5200 and s["verified"] == 1
    assert s["notes"] == "sharp on macro"
    assert s["region"] == "" and s["affiliation"] == ""  # untouched defaults


def test_upsert_only_provided_nonempty_fields_overwrite(conn):
    ing.ingest_rows(conn, "accounts", [
        {"source_id": "s1", "bias": "left", "region": "EU",
         "affiliation": "journalist", "topics": "fed", "followers": 10,
         "verified": 1, "notes": "first pass"}])
    res = ing.ingest_rows(conn, "accounts", [
        {"source_id": "s1", "bias": "center", "region": "",
         "topics": "fed rates"}])
    assert res["ok_rows"] == 1
    s = _src(conn, "s1")
    assert s["bias"] == "center"             # provided -> overwritten
    assert s["topics"] == "fed rates"        # provided -> overwritten
    assert s["region"] == "EU"               # provided EMPTY -> kept
    assert s["affiliation"] == "journalist"  # omitted -> kept
    assert (s["followers"], s["verified"]) == (10, 1)  # omitted -> kept
    assert s["notes"] == "first pass"


def test_upsert_never_resets_existing_priors(conn):
    add_source(conn, "vet", alpha=5.0, beta=3.0)
    ing.ingest_rows(conn, "accounts", [{"source_id": "vet", "bias": "left"}])
    s = _src(conn, "vet")
    assert (s["alpha"], s["beta"]) == (5.0, 3.0)  # track record untouched
    assert s["bias"] == "left"


def test_bad_bias_enum_is_row_error_with_line(conn):
    res = ing.ingest_rows(conn, "accounts", [
        {"source_id": "a", "bias": "center"},
        {"source_id": "b", "bias": "far-left"}])
    assert res["ok_rows"] == 1 and res["err_rows"] == 1
    assert res["errors"] == [{"line": 2, "reason":
        "bias must be one of left|lean-left|center|lean-right|right|unknown,"
        " got 'far-left'"}]
    assert _src(conn, "b") is None  # a bad row never even creates the source


def test_followers_must_be_non_negative_int(conn):
    res = ing.ingest_rows(conn, "accounts", [
        {"source_id": "a", "followers": -3},       # line 1: negative
        {"source_id": "b", "followers": "12.5"},   # line 2: not an integer
        {"source_id": "c", "followers": "12000"},  # line 3: ok (CSV-style)
        {"source_id": "d", "followers": 0}])       # line 4: ok, zero allowed
    assert res["ok_rows"] == 2 and res["err_rows"] == 2
    assert [e["line"] for e in res["errors"]] == [1, 2]
    assert all("non-negative integer" in e["reason"] for e in res["errors"])
    assert _src(conn, "c")["followers"] == 12000
    assert _src(conn, "d")["followers"] == 0
    assert _src(conn, "a") is None


def test_verified_accepts_0_1_true_false(conn):
    res = ing.ingest_rows(conn, "accounts", [
        {"source_id": "a", "verified": "true"},
        {"source_id": "b", "verified": False},   # JSON booleans work too
        {"source_id": "c", "verified": "1"},
        {"source_id": "d", "verified": 0},
        {"source_id": "e", "verified": "yes"}])  # line 5: bad
    assert res["ok_rows"] == 4 and res["err_rows"] == 1
    assert res["errors"][0]["line"] == 5
    assert "verified must be 0|1|true|false" in res["errors"][0]["reason"]
    assert [_src(conn, s)["verified"] for s in "abcd"] == [1, 0, 1, 0]


ACCOUNTS_CSV = (
    "source_id,bias,region,affiliation,topics,followers,verified,notes\n"
    "staffer,lean-right,US-DC,staffer,midterms house djt,12000,1,hill contact\n"
    "trader,,,trader,fed rates,,,\n"
)


def test_accounts_csv_and_clean_sha_short_circuit(conn):
    first = ing.ingest_file(conn, "accounts", ACCOUNTS_CSV.encode(), "a.csv")
    assert first["ok_rows"] == 2 and first["already_ingested"] is False
    s = _src(conn, "staffer")
    assert s["bias"] == "lean-right" and s["followers"] == 12000
    t = _src(conn, "trader")
    assert t["bias"] == "unknown"        # empty bias -> default kept
    assert t["affiliation"] == "trader" and t["followers"] is None
    again = ing.ingest_file(conn, "accounts", ACCOUNTS_CSV.encode(), "b.csv")
    assert again["already_ingested"] is True  # prior run was clean
    assert dict(_src(conn, "staffer")) == dict(s)  # same file, same end state


def test_accounts_with_errors_reprocesses_to_same_end_state(conn):
    csv_text = "source_id,bias\ngood,center\nbad,purple\n"
    r1 = ing.ingest_file(conn, "accounts", csv_text.encode(), "mix.csv")
    assert (r1["ok_rows"], r1["err_rows"]) == (1, 1)
    r2 = ing.ingest_file(conn, "accounts", csv_text.encode(), "mix.csv")
    assert r2["already_ingested"] is False        # errors -> never sealed
    assert (r2["ok_rows"], r2["err_rows"]) == (1, 1)  # errors visible again
    assert _src(conn, "good")["bias"] == "center"     # same end state
    assert _src(conn, "bad") is None


def test_accounts_template_ingest_endpoint_and_raw(client):
    res = client.get("/templates/accounts.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == \
        "source_id,bias,region,affiliation,topics,followers,verified,notes"
    up = client.post("/ingest/accounts", json={"rows": [
        {"source_id": "x", "bias": "lean-left", "region": "US-NY",
         "topics": "djt midterms", "followers": 12, "verified": 1,
         "notes": "beat reporter"}]})
    assert up.json()["ok_rows"] == 1
    raw = client.get("/raw/accounts").json()
    assert raw["total"] == 1
    row = raw["rows"][0]
    assert set(row) == {"source_id", "name", "kind", "bias", "region",
                        "affiliation", "topics", "followers", "verified",
                        "notes"}
    assert row["source_id"] == "x" and row["bias"] == "lean-left"
    assert row["notes"] == "beat reporter"
    assert client.get("/raw/accounts",
                      params={"source_id": "nope"}).json()["total"] == 0


def test_sources_rows_and_detail_gain_context_fields(client):
    client.post("/ingest/accounts", json={"rows": [
        {"source_id": "x", "bias": "right", "region": "US-TX",
         "affiliation": "trader", "topics": "fed", "followers": 7,
         "verified": "true", "notes": "private notes"}]})
    row = client.get("/sources").json()[0]
    assert row["bias"] == "right" and row["region"] == "US-TX"
    assert row["affiliation"] == "trader" and row["topics"] == "fed"
    assert row["followers"] == 7 and row["verified"] is True
    assert "notes" not in row  # notes only on the detail view
    assert client.get("/sources/x").json()["notes"] == "private notes"


# --- relevance: context_match + relevant_sources ------------------------------

def test_context_match_hand_cases():
    q = "trump-2026-rally-tour Trump holds 20+ rallies before the 2026 midterms"
    assert ctx.context_match(q, "midterms") == 1.0            # exact token
    assert ctx.context_match(q, "djt midterms fed") == 1 / 3  # partial overlap
    assert ctx.context_match(q, "crypto") == 0.0              # zero overlap
    assert ctx.context_match(q, "") == 0.0                    # no topics
    assert ctx.context_match(q, "MIDTERMS, Trump") == 1.0     # case + commas
    assert ctx.context_match("midterm-house", "midterms") == 0.0  # no stemming


def test_relevant_sources_shape_order_and_exclusion(conn):
    add_question(conn, "q-djt", title="Trump rally tour")
    ing.ingest_rows(conn, "accounts", [
        {"source_id": "predictor", "topics": "trump rally"},
        {"source_id": "full", "topics": "trump rally"},
        {"source_id": "half_hi", "topics": "trump crypto", "bias": "left",
         "region": "US"},
        {"source_id": "half_lo", "topics": "trump fed"},
        {"source_id": "zero", "topics": "crypto"},
        {"source_id": "no_topics", "notes": "context-less"}])
    conn.execute("UPDATE sources SET alpha = 6, beta = 2 WHERE id = 'half_hi'")
    conn.commit()
    add_prediction(conn, "predictor", "q-djt", 0.8)
    out = ctx.relevant_sources(conn, "q-djt", "q-djt Trump rally tour")
    # predictor excluded (already heard from); zero/no_topics never match;
    # sorted by (match desc, credibility desc)
    assert [r["source_id"] for r in out] == ["full", "half_hi", "half_lo"]
    assert out[0] == {"source_id": "full", "credibility": 0.5,
                      "bias": "unknown", "region": "", "match": 1.0}
    assert out[1]["match"] == 0.5 and out[1]["credibility"] == 0.75
    assert out[1]["bias"] == "left" and out[1]["region"] == "US"


def test_relevant_sources_caps_at_ten(conn):
    add_question(conn, "q1", title="the fed decision")
    ing.ingest_rows(conn, "accounts", [
        {"source_id": f"s{i:02d}", "topics": "fed"} for i in range(12)])
    out = ctx.relevant_sources(conn, "q1", "q1 the fed decision")
    assert len(out) == 10
    assert [r["source_id"] for r in out] == [f"s{i:02d}" for i in range(10)]


# --- the DJT rule --------------------------------------------------------------

DJT_TEXT = "djt-q Trump holds rallies"


def test_djt_detection_on_id_and_title(conn):
    for q_text, expect in [
        ("plain-q Trump rally count", True),           # title hit
        ("djt-approval-2026 Approval steady", True),   # id hit
        ("maga-q THE MAGA MOVEMENT GROWS", True),      # case-insensitive
        ("gala-q Mar-a-Lago dinner happens", True),
        ("boring-q Fed cuts in March", False)]:
        got = ctx.source_context(conn, "q", q_text)["djt_related"]
        assert got is expect, q_text


def test_skew_note_fires_with_exact_string(conn):
    add_question(conn, "djt-q", title="Trump holds rallies")
    _pred_src(conn, "djt-q", "a", "lean-right")
    _pred_src(conn, "djt-q", "b", "lean-right")
    _pred_src(conn, "djt-q", "c", "center")
    out = ctx.source_context(conn, "djt-q", DJT_TEXT)
    # 3 predicting, djt-related, 2 of 3 known-bias = 66.7% > 60%
    assert out["skew_note"] == ("2 of 3 known-bias sources lean lean-right"
                                " — weigh accordingly")


def test_skew_ratio_excludes_unknown_but_they_count_toward_three(conn):
    add_question(conn, "djt-q", title="Trump holds rallies")
    _pred_src(conn, "djt-q", "a", "right")
    _pred_src(conn, "djt-q", "b", "right")
    _pred_src(conn, "djt-q", "c", "unknown")  # 3rd predictor, not in M
    out = ctx.source_context(conn, "djt-q", DJT_TEXT)
    assert out["skew_note"] == ("2 of 2 known-bias sources lean right"
                                " — weigh accordingly")


def test_skew_note_none_below_three_predicting(conn):
    add_question(conn, "djt-q", title="Trump holds rallies")
    _pred_src(conn, "djt-q", "a", "right")
    _pred_src(conn, "djt-q", "b", "right")   # 100% skew but only 2 predicting
    assert ctx.source_context(conn, "djt-q", DJT_TEXT)["skew_note"] is None


def test_skew_note_none_at_exactly_60_percent(conn):
    add_question(conn, "djt-q", title="Trump holds rallies")
    for sid, bias in [("a", "left"), ("b", "left"), ("c", "left"),
                      ("d", "right"), ("e", "right")]:
        _pred_src(conn, "djt-q", sid, bias)
    # 3 of 5 = 60% exactly -> NOT > 60% -> no note
    assert ctx.source_context(conn, "djt-q", DJT_TEXT)["skew_note"] is None


def test_skew_note_none_when_not_djt_related(conn):
    add_question(conn, "fed-q", title="Fed cuts rates")
    for sid in ("a", "b", "c"):
        _pred_src(conn, "fed-q", sid, "right")  # total skew, but not DJT
    out = ctx.source_context(conn, "fed-q", "fed-q Fed cuts rates")
    assert out["djt_related"] is False and out["skew_note"] is None


def test_skew_note_none_when_all_bias_unknown(conn):
    add_question(conn, "djt-q", title="Trump holds rallies")
    for sid in ("a", "b", "c"):
        _pred_src(conn, "djt-q", sid, "unknown")
    assert ctx.source_context(conn, "djt-q", DJT_TEXT)["skew_note"] is None


def test_bias_and_region_spreads_counted_over_predicting(conn):
    add_question(conn, "q1", title="Senate control")
    for sid, bias, region in [("a", "left", "US-NY"),
                              ("b", "lean-right", "US-DC"),
                              ("c", "lean-right", "US-DC"),
                              ("d", "unknown", ""),      # '' -> 'unknown'
                              ("e", "center", "EU")]:
        _pred_src(conn, "q1", sid, bias, region)
    ing.ingest_rows(conn, "accounts", [   # bystander: context, no prediction
        {"source_id": "bystander", "bias": "right", "region": "MARS"}])
    out = ctx.source_context(conn, "q1", "q1 Senate control")
    assert out["bias_spread"] == {"left": 1, "lean-left": 0, "center": 1,
                                  "lean-right": 2, "right": 0, "unknown": 1}
    assert out["region_spread"] == {"US-DC": 2, "EU": 1, "US-NY": 1,
                                    "unknown": 1}  # bystander not counted


def test_region_spread_keeps_top_five_only(conn):
    add_question(conn, "q1", title="Senate control")
    for i, region in enumerate(["R1", "R2", "R3", "R4", "R5", "R6"]):
        _pred_src(conn, "q1", f"s{i}", "center", region)
    _pred_src(conn, "q1", "extra", "center", "R1")  # R1 -> 2
    spread = ctx.source_context(conn, "q1", "q1 Senate control")["region_spread"]
    assert len(spread) == 5 and spread["R1"] == 2
    assert "R6" not in spread  # count ties break alphabetically, R6 drops


def test_question_detail_context_fields_via_api(client):
    client.post("/ingest/predictions", json={"rows": [
        {"source_id": "s1", "question_id": "djt-rally",
         "predicted_probability": 0.7, "stated_at": "2026-08-01T12:00:00Z"}]})
    client.post("/ingest/accounts", json={"rows": [
        {"source_id": "s1", "bias": "lean-right", "region": "US-DC"},
        {"source_id": "watcher", "topics": "djt rally", "bias": "center"}]})
    body = client.get("/questions/djt-rally").json()
    sc = body["source_context"]
    assert set(sc) == {"bias_spread", "region_spread", "djt_related",
                       "skew_note"}
    assert sc["djt_related"] is True
    assert sc["bias_spread"]["lean-right"] == 1
    assert sc["region_spread"] == {"US-DC": 1}
    assert sc["skew_note"] is None  # only 1 predicting source
    rel = body["relevant_sources"]  # who we should hear from but aren't
    assert [r["source_id"] for r in rel] == ["watcher"]
    assert rel[0]["match"] == 1.0 and rel[0]["bias"] == "center"


# --- sample data v3 -------------------------------------------------------------

def test_sample_v3_context_and_double_load_idempotent(client):
    first = client.post("/sample/load").json()
    assert first["counts"] == {"sources": 6, "questions": 15,
                               "predictions": 17, "snapshots": 13}
    again = client.post("/sample/load").json()
    assert again["counts"] == first["counts"]  # double-load identical counts
    srcs = {r["id"]: r for r in client.get("/sources").json()}
    for sid in ("race_model", "poll_aggregator", "state_polls",
                "macro_model", "generic_ballot"):
        s = srcs[f"sample:{sid}"]
        assert s["bias"] == "center" and s["region"] == "US"
        assert s["affiliation"] == "model"
        assert s["topics"] == "midterms polls senate house"
    staff = srcs["sample:capitol_staffer"]
    assert staff["bias"] == "lean-right" and staff["region"] == "US-DC"
    assert staff["affiliation"] == "staffer"
    assert staff["topics"] == "midterms house djt"


def test_sample_djt_question_demoable(client):
    client.post("/sample/load")
    body = client.get("/questions/trump-2026-rally-tour").json()
    q = body["question"]
    assert q["title"] == "Trump holds 20+ rallies before the 2026 midterms"
    assert q["status"] == "live" and q["is_sample"] is True
    ps = {r["source_id"]: r["p"] for r in body["per_source"]}
    assert ps == {"sample:capitol_staffer": 0.7, "sample:race_model": 0.55}
    assert body["market"][0]["venue"] == "polymarket"
    assert body["market"][0]["yes_price"] == 0.6
    assert body["market"][0]["liquidity"] == 30000.0
    sc = body["source_context"]
    assert sc["djt_related"] is True
    assert sc["bias_spread"] == {"left": 0, "lean-left": 0, "center": 1,
                                 "lean-right": 1, "right": 0, "unknown": 0}
    assert sc["region_spread"] == {"US": 1, "US-DC": 1}
    assert sc["skew_note"] is None  # only 2 predicting sources (< 3)
    rel = body["relevant_sources"]  # 4 models not yet on it; .25 tie -> cred
    assert [r["source_id"] for r in rel] == [
        "sample:macro_model", "sample:poll_aggregator",
        "sample:generic_ballot", "sample:state_polls"]
    assert all(r["match"] == 0.25 for r in rel)  # "midterms" of 4 topic tokens
