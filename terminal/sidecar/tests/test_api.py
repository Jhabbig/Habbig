"""Pins the exact API shapes from CONTRACT.md against a live TestClient."""


def test_health(client, db_file):
    body = client.get("/health").json()
    assert body == {"ok": True, "version": "0.5.0", "db": db_file}


def test_sample_load_counts_and_idempotence(client):
    body = client.post("/sample/load").json()
    assert body["loaded"] is True
    assert body["counts"] == {"sources": 5, "questions": 14,
                              "predictions": 14, "snapshots": 12}
    again = client.post("/sample/load").json()
    assert again["counts"] == body["counts"]  # second load is a no-op


def test_sources_shape_order_and_movement(client):
    client.post("/sample/load")
    rows = client.get("/sources").json()
    assert len(rows) == 5
    creds = [r["credibility"] for r in rows]
    assert creds == sorted(creds, reverse=True)  # cred desc
    keys = {"id", "name", "alpha", "beta", "credibility", "n_resolved",
            "n_live", "brier", "is_sample", "last_active"}
    assert set(rows[0]) == keys
    by_id = {r["id"]: r for r in rows}
    race = by_id["sample:race_model"]
    assert race["alpha"] == 13.0 and race["beta"] == 3.0  # 12/3 + resolved hit
    assert race["credibility"] == 0.8125
    assert race["n_resolved"] == 1 and race["n_live"] == 3
    assert race["brier"] == round((0.72 - 1) ** 2, 4)
    poll = by_id["sample:poll_aggregator"]
    assert poll["beta"] == 6.0 and poll["credibility"] == 0.76  # miss recorded
    assert by_id["sample:state_polls"]["brier"] is None


def test_source_detail_events_and_predictions(client):
    client.post("/sample/load")
    body = client.get("/sources/sample:race_model").json()
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["old_alpha"] == 12.0 and ev["new_alpha"] == 13.0
    assert ev["question_id"] == "va-gov-dem-2025"
    assert len(body["predictions"]) == 4
    assert client.get("/sources/nope").status_code == 404


def test_questions_shape_and_edge_sort(client):
    client.post("/sample/load")
    rows = client.get("/questions").json()
    assert len(rows) == 14
    keys = {"id", "title", "status", "n_sources", "combined_p", "market_price",
            "edge", "is_sample", "updated_at"}
    assert set(rows[0]) == keys
    edges = [abs(r["edge"]) for r in rows if r["edge"] is not None]
    assert edges == sorted(edges, reverse=True)  # |edge| desc
    assert all(r["edge"] is None for r in rows[len(edges):])  # None edges last
    top = rows[0]
    assert top["id"] == "midterm-house-gop-2026" and top["edge"] == 0.07
    assert top["n_sources"] == 1 and top["is_sample"] is True


def test_question_detail_shape(client):
    client.post("/sample/load")
    body = client.get("/questions/midterm-house-gop-2026").json()
    assert set(body) == {"question", "per_source", "combined_p", "market",
                         "history"}
    assert body["question"]["status"] == "live"
    assert body["combined_p"] == 0.62  # single source -> blend == its p
    assert body["per_source"] == [{"source_id": "sample:race_model",
                                   "credibility": 0.8125, "p": 0.62,
                                   "stated_at": body["per_source"][0]["stated_at"]}]
    assert len(body["market"]) == 1
    assert body["market"][0]["venue"] == "polymarket"
    assert body["market"][0]["yes_price"] == 0.55
    assert len(body["history"]) == 1
    assert client.get("/questions/nope").status_code == 404


def test_resolve_endpoint_and_409(client):
    client.post("/sample/load")
    res = client.post("/resolve", json={"question_id": "midterm-house-gop-2026",
                                        "outcome": "yes"})
    assert res.status_code == 200
    body = res.json()
    assert body["resolved"] is True
    assert body["moves"] == [{"source_id": "sample:race_model",
                              "old_cred": 0.8125,
                              "new_cred": round(14 / 17, 4), "hit": True}]
    again = client.post("/resolve", json={"question_id": "midterm-house-gop-2026",
                                          "outcome": "no"})
    assert again.status_code == 409
    assert client.post("/resolve", json={"question_id": "nope",
                                         "outcome": "yes"}).status_code == 404
    assert client.post("/resolve", json={"question_id": "midterm-tx-sen-gop-2026",
                                         "outcome": "maybe"}).status_code == 422


def test_resolve_void_endpoint(client):
    client.post("/sample/load")
    body = client.post("/resolve", json={"question_id": "midterm-tx-sen-gop-2026",
                                         "outcome": "void"}).json()
    assert body == {"resolved": True, "moves": []}


def test_ingest_endpoint_multipart_and_json(client):
    csv_bytes = (b"source_id,question_id,predicted_probability,stated_at,note\n"
                 b"a,q1,0.7,2026-08-01T12:00:00Z,\n")
    res = client.post("/ingest/predictions",
                      files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert res.status_code == 200
    assert res.json()["ok_rows"] == 1
    res2 = client.post("/ingest/predictions", json={"rows": [
        {"source_id": "a", "question_id": "q1",
         "predicted_probability": 0.8, "stated_at": "2026-08-02T12:00:00Z"}]})
    assert res2.json()["ok_rows"] == 1
    assert client.post("/ingest/nope", json={"rows": []}).status_code == 404
    bad = client.post("/ingest/predictions",
                      files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")})
    assert bad.status_code == 400
    assert "missing required column" in bad.json()["detail"]


def test_templates_endpoint(client):
    res = client.get("/templates/predictions.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text.splitlines()[0] == \
        "source_id,question_id,predicted_probability,stated_at,note"
    assert client.get("/templates/nope.csv").status_code == 404


def test_raw_endpoint(client):
    client.post("/sample/load")
    body = client.get("/raw/predictions").json()
    assert set(body) == {"rows", "total"}
    assert body["total"] == 14 and len(body["rows"]) == 14
    filtered = client.get(
        "/raw/predictions",
        params={"source_id": "sample:state_polls", "limit": 3}).json()
    assert filtered["total"] == 6 and len(filtered["rows"]) == 3
    markets = client.get("/raw/markets",
                         params={"question_id": "midterm-house-gop-2026"}).json()
    assert markets["total"] == 1
    assert markets["rows"][0]["venue"] == "polymarket"
    resolutions = client.get("/raw/resolutions").json()
    assert resolutions["total"] == 2
    outcomes = {r["question_id"]: r["outcome"] for r in resolutions["rows"]}
    assert outcomes == {"va-gov-dem-2025": "yes", "nj-gov-gop-2025": "no"}
    assert client.get("/raw/nope").status_code == 404
