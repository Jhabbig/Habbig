"""Tests for the multi-source info ingest layer.

Two tiers:
  * PARSE-ONLY (default, offline, deterministic): feed each parser a saved
    fixture string captured live from the real source and assert the
    normalised shape. No network — these always run.
  * LIVE (marked `network`): actually hit the four reachable sources and assert
    we can pull >=1 normalised item from at least one of them. Reddit/HN/NPR/
    Hill genuinely respond from this host, but the IP is flaky, so the live
    assertions are tolerant (per-source may be 0; the union must be non-empty).

Run only fast offline tests:  pytest tests/test_info_ingest.py
Run the live smoke too:        pytest -m network tests/test_info_ingest.py
"""
import pytest

from intelligence import info_ingest as ing


# --------------------------------------------------------------------------- #
# Fixtures captured live from each real source (trimmed to the relevant shape).
# --------------------------------------------------------------------------- #
REDDIT_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <category term="politics" label="r/politics"/>
  <updated>2026-06-18T17:06:16+00:00</updated>
  <id>/r/politics/search.rss?q=election</id>
  <entry>
    <author><name>/u/somevoter</name></author>
    <title>Election turnout projections climb in three swing states</title>
    <link rel="alternate" href="https://www.reddit.com/r/politics/comments/abc123/election_turnout/" />
    <updated>2026-06-18T16:40:00+00:00</updated>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Early voting numbers are &lt;b&gt;way up&lt;/b&gt; vs 2022.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
  </entry>
  <entry>
    <author><name>/u/anotheruser</name></author>
    <title>Second post about the race</title>
    <link rel="alternate" href="https://www.reddit.com/r/politics/comments/def456/second/" />
    <updated>2026-06-18T16:10:00+00:00</updated>
    <content type="html">&lt;p&gt;Ground game looks strong.&lt;/p&gt;</content>
  </entry>
</feed>
"""

HN_JSON = """{
  "hits": [
    {
      "title": "Modeling the 2026 midterm outcome",
      "story_text": "Here is my <i>open-source</i> model and the data behind it.",
      "url": "https://example.com/model",
      "author": "forecaster",
      "points": 42,
      "created_at": "2026-06-18T12:00:00.000Z"
    },
    {
      "title": "Ask HN: best polling aggregators?",
      "story_text": "",
      "url": null,
      "author": "curious",
      "points": 3,
      "created_at": "2026-06-18T11:00:00.000Z"
    }
  ]
}"""

# NPR/Hill RSS 2.0 — note dc:creator namespace and CDATA descriptions.
RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>NPR Topics: Politics</title>
    <link>https://www.npr.org/</link>
    <item>
      <title>Senator on confirmation hearing and election integrity</title>
      <description>NPR&apos;s host talks to a senator about the fight over the confirmation of a new intelligence director and election integrity.</description>
      <pubDate>Thu, 18 Jun 2026 06:51:07 -0400</pubDate>
      <link>https://www.npr.org/2026/06/18/story-one</link>
      <guid>https://www.npr.org/2026/06/18/story-one</guid>
    </item>
    <item>
      <title>Vance pushes back on GOP critics of Iran deal</title>
      <dc:creator><![CDATA[Julia Manchester]]></dc:creator>
      <pubDate>Thu, 18 Jun 2026 17:02:02 +0000</pubDate>
      <link>https://thehill.com/homenews/administration/5930505-vance/</link>
      <description><![CDATA[Vice President Vance pushed back on criticism of the <b>memorandum</b> with Iran.]]></description>
    </item>
  </channel>
</rss>
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _assert_item_shape(it: dict):
    assert set(it) == {"platform", "text", "author", "url", "ts"}
    assert isinstance(it["platform"], str) and it["platform"]
    assert isinstance(it["text"], str) and it["text"].strip()
    assert it["author"] is None or isinstance(it["author"], str)
    assert it["url"] is None or isinstance(it["url"], str)
    assert it["ts"] is None or isinstance(it["ts"], str)
    # text must be free of actual HTML *tags* (bare angle brackets in prose,
    # e.g. "probe->kernel", are fine — only <tag> patterns are forbidden).
    assert not ing._TAG_RE.search(it["text"])


# --------------------------------------------------------------------------- #
# Pure-function helpers
# --------------------------------------------------------------------------- #
def test_strip_html_removes_tags_and_entities():
    assert ing._strip_html("<p>hello &amp; <b>bye</b></p>") == "hello & bye"
    assert ing._strip_html("") == ""
    assert ing._strip_html(None) == ""
    assert ing._strip_html("a\n\n  b   c") == "a b c"


# --------------------------------------------------------------------------- #
# Parse-only (offline, deterministic)
# --------------------------------------------------------------------------- #
def test_parse_reddit_fixture():
    items = ing.parse_reddit(REDDIT_ATOM)
    assert len(items) == 2
    for it in items:
        _assert_item_shape(it)
        assert it["platform"] == "reddit"
    first = items[0]
    assert "Election turnout projections" in first["text"]
    assert "Early voting numbers are way up" in first["text"]  # HTML stripped
    assert first["author"] == "/u/somevoter"
    assert first["url"].endswith("/election_turnout/")
    assert first["ts"] == "2026-06-18T16:40:00+00:00"


def test_parse_reddit_limit_and_empty():
    assert ing.parse_reddit(REDDIT_ATOM, limit=1) == ing.parse_reddit(REDDIT_ATOM)[:1]
    assert ing.parse_reddit("") == []
    assert ing.parse_reddit("not xml at all <<<") == []


def test_parse_hackernews_fixture():
    items = ing.parse_hackernews(HN_JSON)
    assert len(items) == 2
    for it in items:
        _assert_item_shape(it)
        assert it["platform"] == "hackernews"
    assert "Modeling the 2026 midterm outcome" in items[0]["text"]
    assert "open-source" in items[0]["text"] and "<i>" not in items[0]["text"]
    assert items[0]["author"] == "forecaster"
    assert items[0]["url"] == "https://example.com/model"
    assert items[0]["ts"] == "2026-06-18T12:00:00.000Z"
    # second hit has empty story_text + null url -> still valid (title only)
    assert items[1]["url"] is None
    assert items[1]["text"] == "Ask HN: best polling aggregators?"


def test_parse_hackernews_empty_and_bad():
    assert ing.parse_hackernews("") == []
    assert ing.parse_hackernews("{not json") == []
    assert ing.parse_hackernews('{"hits": []}') == []


def test_parse_rss_fixture():
    items = ing.parse_rss(RSS_2)
    assert len(items) == 2
    for it in items:
        _assert_item_shape(it)
        assert it["platform"] == "news"
    assert "election integrity" in items[0]["text"]
    assert items[0]["url"] == "https://www.npr.org/2026/06/18/story-one"
    assert items[0]["ts"] == "Thu, 18 Jun 2026 06:51:07 -0400"
    # dc:creator parsed, CDATA + HTML stripped from description
    assert items[1]["author"] == "Julia Manchester"
    assert "memorandum" in items[1]["text"] and "<b>" not in items[1]["text"]


def test_parse_rss_empty_and_bad():
    assert ing.parse_rss("") == []
    assert ing.parse_rss("<rss><channel></channel></rss>") == []
    assert ing.parse_rss("garbage") == []


def test_gather_dedupes(monkeypatch):
    """gather() must combine sources and dedupe on (platform, text[:120]),
    and survive a source that raises."""
    dup = {"platform": "reddit", "text": "same headline", "author": None,
           "url": None, "ts": None}
    monkeypatch.setattr(ing, "fetch_reddit", lambda sub, query=None, limit=15: [dup, dict(dup)])
    monkeypatch.setattr(ing, "fetch_hackernews",
                        lambda q, limit=15: [{"platform": "hackernews", "text": "hn item",
                                              "author": None, "url": None, "ts": None}])
    def _boom(limit=15):
        raise RuntimeError("source down")
    monkeypatch.setattr(ing, "fetch_npr", _boom)        # must not crash gather
    monkeypatch.setattr(ing, "fetch_hill", lambda limit=15: [])
    # New sources added to gather() — stub them so this stays a unit test (no
    # network): google_news raises (gather must survive it), bbc/guardian empty.
    monkeypatch.setattr(ing, "fetch_google_news", lambda q, limit=15: (_ for _ in ()).throw(RuntimeError("source down")))
    monkeypatch.setattr(ing, "fetch_bbc", lambda limit=15: [])
    monkeypatch.setattr(ing, "fetch_guardian", lambda limit=15: [])
    out = ing.gather("anything", limit_per=15)
    # 2 reddit dupes collapse to 1; + 1 hn = 2 total; npr + google_news raised
    # (skipped, gather survives); hill/bbc/guardian empty.
    assert len(out) == 2
    platforms = {it["platform"] for it in out}
    assert platforms == {"reddit", "hackernews"}


def test_curl_retries_on_empty(monkeypatch):
    """_curl must retry on an empty body and eventually return ""."""
    calls = {"n": 0}

    class _Proc:
        returncode = 0
        stdout = ""  # always empty -> exhaust retries

    def _fake_run(cmd, capture_output, text):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr(ing.subprocess, "run", _fake_run)
    monkeypatch.setattr(ing.time, "sleep", lambda s: None)  # no real waiting
    assert ing._curl("http://x", retries=3) == ""
    assert calls["n"] == 3  # retried the full budget on empty bodies


def test_curl_returns_first_nonempty(monkeypatch):
    bodies = ["", "  ", "<feed>ok</feed>"]
    idx = {"i": 0}

    class _Proc:
        def __init__(self, out):
            self.returncode = 0
            self.stdout = out

    def _fake_run(cmd, capture_output, text):
        out = bodies[idx["i"]]
        idx["i"] += 1
        return _Proc(out)

    monkeypatch.setattr(ing.subprocess, "run", _fake_run)
    monkeypatch.setattr(ing.time, "sleep", lambda s: None)
    assert ing._curl("http://x", retries=5) == "<feed>ok</feed>"


def test_ingested_items_feed_forecast_engine():
    """End-to-end shape contract: parsed items are accepted by info_forecast."""
    import json
    from intelligence import info_forecast as fc

    items = ing.parse_reddit(REDDIT_ATOM) + ing.parse_rss(RSS_2)

    def _llm(system, user):
        assert "QUESTION:" in user and "INFO ITEMS:" in user
        return json.dumps({"probability_yes": 0.61, "confidence": 0.5,
                           "digest": "d", "drivers": [], "political_lean": "center"})

    out = fc.forecast("Will turnout rise?", items, llm=_llm)
    assert out["probability_yes"] == 0.61
    assert out["n_items"] == len(items)
    # hackernews/reddit/news all map into known spread buckets
    assert set(out["source_spread"]) == set(fc.KNOWN_PLATFORMS)


# --------------------------------------------------------------------------- #
# LIVE (network) — real sources, tolerant of this IP's flakiness.
# --------------------------------------------------------------------------- #
@pytest.mark.network
def test_live_hackernews():
    items = ing.fetch_hackernews("election", limit=5)
    assert isinstance(items, list)
    for it in items:
        _assert_item_shape(it)
        assert it["platform"] == "hackernews"


@pytest.mark.network
def test_live_news_feeds():
    npr = ing.fetch_npr(limit=5)
    hill = ing.fetch_hill(limit=5)
    for it in npr + hill:
        _assert_item_shape(it)
        assert it["platform"] == "news"
    # NPR + Hill are reliably reachable from this host; union must be non-empty.
    assert (len(npr) + len(hill)) >= 1, "NPR+Hill returned nothing — feeds down?"


@pytest.mark.network
def test_live_reddit_shape_tolerant():
    # Reddit is the flakiest source from this IP (empty bodies). Don't require
    # items, but anything returned must be well-formed.
    items = ing.fetch_reddit("politics", query="election", limit=5)
    assert isinstance(items, list)
    for it in items:
        _assert_item_shape(it)
        assert it["platform"] == "reddit"


@pytest.mark.network
def test_live_gather_union_nonempty():
    # The whole point: across all reachable sources, a topical query yields info.
    out = ing.gather("election", limit_per=8)
    for it in out:
        _assert_item_shape(it)
    assert len(out) >= 1, "gather() returned nothing across all 4 sources"
