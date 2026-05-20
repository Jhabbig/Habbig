"""Speech diff viewer — v1.2.

For each regulator, pick the two most-recent `speech`-tagged items from
the feed and compute a token-level diff over the concatenated title +
summary. Reuses the v0.1 classifier output — no new ingestion code.

Scope note:
  In v1.2 we diff RSS-level text only (title + summary, typically
  200–600 chars). Summaries are punchy but short — diffs surface
  headline + lede-paragraph changes, not deep prose shifts. Full-body
  HTML fetching (matching
  `centralbank-dashboard/ingestion/cb_statements._fetch_statement_body`)
  is the natural polish lift, deferred until v1.2 has live usage telling
  us whether summary-level diffs carry enough signal.
"""

from __future__ import annotations

import difflib
import re

# Tokenize keeping whitespace + punctuation as separate tokens so the
# renderer can reconstruct readable text by concatenating them back.
_SPLIT_RX = re.compile(r"(\s+|[.,;:!?\"'(){}\[\]])")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in _SPLIT_RX.split(text) if t]


def _speech_text(item: dict) -> str:
    return (item.get("title", "") + ". " + item.get("summary", "")).strip()


def _compact_diff(a_text: str, b_text: str) -> dict:
    a_tokens = tokenize(a_text)
    b_tokens = tokenize(b_text)
    matcher = difflib.SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
    ops: list[dict] = []
    added = removed = equal = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        a_chunk = a_tokens[i1:i2]
        b_chunk = b_tokens[j1:j2]
        ops.append({"op": op, "a": a_chunk, "b": b_chunk})
        # Stats count only non-whitespace tokens so "  " doesn't inflate.
        a_words = sum(1 for t in a_chunk if not t.isspace() and t.strip())
        b_words = sum(1 for t in b_chunk if not t.isspace() and t.strip())
        if op == "equal":
            equal += a_words
        elif op == "delete":
            removed += a_words
        elif op == "insert":
            added += b_words
        elif op == "replace":
            removed += a_words
            added += b_words
    return {
        "ops": ops,
        "stats": {"added": added, "removed": removed, "equal": equal},
        "ratio": round(matcher.ratio(), 3),
    }


def _short(it: dict) -> dict:
    return {
        "id":        it.get("id"),
        "title":     it.get("title"),
        "link":      it.get("link"),
        "published": it.get("published"),
    }


def latest_vs_prior(items: list[dict], regulator_code: str) -> dict:
    """Pick the two most-recent speech-tagged items from this regulator
    and diff them. Returns a structured payload; `diff` is None if fewer
    than two speeches sit in the window."""
    speeches = sorted(
        (it for it in items
         if it.get("source") == regulator_code
         and it.get("primary_tag") == "speech"),
        key=lambda x: x.get("published") or "",
        reverse=True,
    )
    if not speeches:
        return {"regulator": regulator_code, "latest": None, "prior": None, "diff": None}
    if len(speeches) == 1:
        return {
            "regulator": regulator_code,
            "latest": _short(speeches[0]),
            "prior": None,
            "diff": None,
        }
    latest, prior = speeches[0], speeches[1]
    return {
        "regulator": regulator_code,
        "latest": _short(latest),
        "prior":  _short(prior),
        "diff":   _compact_diff(_speech_text(prior), _speech_text(latest)),
    }


def compute_all(items: list[dict],
                regulators: tuple[str, ...] = ("SEC", "FCA", "ESMA")) -> list[dict]:
    return [latest_vs_prior(items, r) for r in regulators]


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    items = [
        {"source": "SEC", "primary_tag": "speech", "published": "2026-05-15",
         "id": "SEC::s1", "title": "Remarks on enforcement priorities",
         "summary": "We will pursue robust enforcement against fraud and hold bad actors accountable."},
        {"source": "SEC", "primary_tag": "speech", "published": "2026-04-15",
         "id": "SEC::s2", "title": "Remarks on enforcement",
         "summary": "We will pursue enforcement against bad actors in our markets."},
        {"source": "FCA", "primary_tag": "speech", "published": "2026-05-10",
         "id": "FCA::s1", "title": "Speech on the Consumer Duty",
         "summary": "Vulnerable consumers need protection."},
        # ESMA — no speech-tagged item
    ]
    out = compute_all(items)
    for d in out:
        if d["diff"]:
            s = d["diff"]["stats"]
            print(f"-- {d['regulator']}: {d['prior']['published']} → {d['latest']['published']}  "
                  f"+{s['added']}/-{s['removed']} words  ratio={d['diff']['ratio']}")
            # Show op summary
            for op in d["diff"]["ops"]:
                if op["op"] == "equal":
                    continue
                a_text = "".join(op["a"]).strip()
                b_text = "".join(op["b"]).strip()
                print(f"   {op['op']:8s}  −{a_text!r:40s} +{b_text!r}")
        elif d["latest"]:
            print(f"-- {d['regulator']}: only one speech ({d['latest']['published']})")
        else:
            print(f"-- {d['regulator']}: no speeches")
    # Spot-check
    sec_diff = next(d for d in out if d["regulator"] == "SEC")
    assert sec_diff["diff"] is not None
    assert sec_diff["diff"]["stats"]["added"] > 0
    assert sec_diff["diff"]["stats"]["removed"] > 0
    fca = next(d for d in out if d["regulator"] == "FCA")
    assert fca["diff"] is None and fca["latest"] is not None
    esma = next(d for d in out if d["regulator"] == "ESMA")
    assert esma["latest"] is None
    print("\nsmoke OK")
