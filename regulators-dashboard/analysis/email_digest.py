"""Email rendering for v1.6 digest.

Two templates:
  - `render_confirmation` — sent on subscribe, with a confirm URL
  - `render_daily_digest` — sent on cron dispatch, with item list and an
    unsubscribe URL

Both emit plain-text and HTML variants. HTML uses inline styles (no
external CSS — most email clients block remote resources). Layout is
deliberately bland: a single column with rules between items, severity
chips inlined as text ("[ENFORCEMENT · SEVERE]"), and no fancy fonts.
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone


def _esc(s: str | None) -> str:
    return _html.escape("" if s is None else str(s), quote=True)


def _short_date(iso: str | None) -> str:
    return (iso or "")[:10]


def _format_filter(filter_dict: dict) -> str:
    """Render the filter dict as a human-readable line ('US · enforcement · severity≥high')."""
    if not filter_dict:
        return "(no filter — all items)"
    parts: list[str] = []
    for key in ("jurisdiction", "source", "tag", "severity", "topic", "q"):
        v = filter_dict.get(key)
        if v:
            parts.append(f"{key}={v}")
    return " · ".join(parts) if parts else "(no filter)"


def render_confirmation(*, email: str, confirm_url: str, filter_dict: dict) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body)."""
    subject = "Confirm your Regulators Dashboard digest subscription"
    filter_line = _format_filter(filter_dict)

    text = (
        f"Hi,\n\n"
        f"Someone (we hope you) signed up {email} for the Regulators Dashboard daily digest.\n\n"
        f"Filter: {filter_line}\n\n"
        f"To confirm and start receiving the digest, click this link:\n\n"
        f"  {confirm_url}\n\n"
        f"If you didn't sign up, ignore this email — without a click here, no further "
        f"emails will be sent.\n"
    )

    html = f"""<!doctype html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;max-width:600px;margin:24px auto;padding:0 16px;line-height:1.5">
<h2 style="margin:0 0 16px;font-size:18px">Confirm your Regulators Dashboard digest subscription</h2>
<p>Someone (we hope you) signed up <strong>{_esc(email)}</strong> for the Regulators Dashboard daily digest.</p>
<p style="background:#f3f4f6;padding:8px 12px;border-radius:4px;font-size:13px;font-family:ui-monospace,monospace">
Filter: {_esc(filter_line)}
</p>
<p style="margin:24px 0"><a href="{_esc(confirm_url)}" style="display:inline-block;background:#1f6feb;color:#fff;text-decoration:none;padding:10px 18px;border-radius:4px;font-weight:600">Confirm subscription</a></p>
<p style="color:#666;font-size:13px">If you didn't sign up, ignore this email — without a click on the button above, no further emails will be sent.</p>
</body></html>"""

    return subject, text, html


def _item_one_line(it: dict) -> str:
    """Plain-text per-item rendering for the text email."""
    bits: list[str] = []
    bits.append(_short_date(it.get("published")))
    bits.append(f"[{it.get('source', '?')}]")
    primary = (it.get("primary_tag") or "").upper()
    if primary and primary != "OTHER":
        sev = it.get("severity")
        if sev and sev.get("bucket"):
            bits.append(f"[{primary} · {sev['bucket'].upper()}]")
        else:
            bits.append(f"[{primary}]")
    bits.append(it.get("title", ""))
    line = " ".join(bits)
    link = it.get("link") or ""
    if link:
        line += f"\n  {link}"
    return line


def _item_html_block(it: dict) -> str:
    title = _esc(it.get("title", ""))
    link = _esc(it.get("link") or "")
    src = _esc(it.get("source", "?"))
    date = _esc(_short_date(it.get("published")))
    primary = (it.get("primary_tag") or "").lower()
    pretty_tag = primary.upper() if primary and primary != "other" else ""
    sev = it.get("severity") or {}
    sev_bucket = (sev.get("bucket") or "").upper()
    summary = _esc(it.get("summary") or "")
    topics = it.get("topics") or []
    chips = []
    if pretty_tag:
        color_map = {
            "enforcement": "#f85149", "rulemaking": "#1f6feb",
            "guidance": "#2ea043", "speech": "#8957e5", "personnel": "#d29922",
        }
        bg = color_map.get(primary, "#6e7681")
        chips.append(
            f'<span style="display:inline-block;padding:1px 6px;border-radius:3px;'
            f'background:{bg};color:#fff;font-size:10px;font-weight:700;letter-spacing:0.04em">'
            f'{pretty_tag}</span>'
        )
    if sev_bucket:
        sev_color = {"low": "#6e7681", "medium": "#d29922", "high": "#f0883e", "severe": "#f85149"}
        chips.append(
            f'<span style="display:inline-block;padding:1px 6px;border-radius:3px;'
            f'background:{sev_color.get(sev_bucket.lower(), "#30363d")};color:#fff;font-size:10px;font-weight:700">'
            f'{sev_bucket}</span>'
        )
    for t in topics[:4]:
        chips.append(
            f'<span style="display:inline-block;padding:1px 6px;border-radius:10px;'
            f'background:#e5e7eb;color:#374151;font-size:10px">{_esc(t)}</span>'
        )

    title_html = f'<a href="{link}" style="color:#1f6feb;text-decoration:none;font-weight:600">{title}</a>' if link else title
    return f"""
<div style="padding:12px 0;border-bottom:1px solid #e5e7eb">
  <div style="color:#6b7280;font-size:12px;margin-bottom:4px">{date} · {src} · {' '.join(chips)}</div>
  <div style="font-size:14px;margin-bottom:4px">{title_html}</div>
  <div style="color:#6b7280;font-size:12px">{summary}</div>
</div>"""


def render_daily_digest(*, email: str, items: list[dict], filter_dict: dict,
                        unsubscribe_url: str, dashboard_url: str = "") -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body)."""
    today = datetime.now(timezone.utc).date().isoformat()
    filter_line = _format_filter(filter_dict)
    n = len(items)
    subject = f"Regulators digest — {today} ({n} item{'s' if n != 1 else ''})"

    if not items:
        text_body = (
            f"Nothing matched your filter today.\n\n"
            f"Filter: {filter_line}\n\n"
            f"Unsubscribe: {unsubscribe_url}\n"
        )
        html_body = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;max-width:700px;margin:24px auto;padding:0 16px;line-height:1.5">
<h2 style="margin:0 0 8px;font-size:18px">Regulators digest — {_esc(today)}</h2>
<div style="color:#6b7280;font-size:12px;margin-bottom:16px">Filter: {_esc(filter_line)}</div>
<p>Nothing matched your filter today.</p>
<p style="color:#9ca3af;font-size:11px;margin-top:32px"><a href="{_esc(unsubscribe_url)}" style="color:#9ca3af">Unsubscribe</a></p>
</body></html>"""
        return subject, text_body, html_body

    text_lines = [
        f"Regulators digest — {today} ({n} item{'s' if n != 1 else ''})",
        f"Filter: {filter_line}",
        "",
    ]
    for it in items:
        text_lines.append(_item_one_line(it))
        text_lines.append("")
    text_lines.append(f"Unsubscribe: {unsubscribe_url}")
    text_body = "\n".join(text_lines)

    item_blocks = "".join(_item_html_block(it) for it in items)
    dashboard_link = (
        f'<a href="{_esc(dashboard_url)}" style="color:#1f6feb;text-decoration:none">Open dashboard ↗</a>'
        if dashboard_url else ""
    )
    html_body = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;max-width:700px;margin:24px auto;padding:0 16px;line-height:1.5">
<h2 style="margin:0 0 8px;font-size:18px">Regulators digest — {_esc(today)}</h2>
<div style="color:#6b7280;font-size:12px;margin-bottom:16px">
  {n} item{'s' if n != 1 else ''} · Filter: {_esc(filter_line)} {f'· {dashboard_link}' if dashboard_link else ''}
</div>
{item_blocks}
<p style="color:#9ca3af;font-size:11px;margin-top:32px"><a href="{_esc(unsubscribe_url)}" style="color:#9ca3af">Unsubscribe</a></p>
</body></html>"""
    return subject, text_body, html_body


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    sub, txt, html = render_confirmation(
        email="alice@example.com",
        confirm_url="https://example.com/api/subscribe/confirm?token=abc",
        filter_dict={"tag": "enforcement", "jurisdiction": "US"},
    )
    assert "Confirm" in sub and "alice@example.com" in txt and "alice@example.com" in html

    items = [
        {
            "title": "SEC charges firm with fraud",
            "link": "https://sec.gov/x",
            "published": "2026-05-15T10:00:00+00:00",
            "summary": "Today announced a $200M penalty.",
            "source": "SEC",
            "primary_tag": "enforcement",
            "severity": {"bucket": "severe", "amount_native": 200_000_000, "currency": "USD"},
            "topics": ["aml", "crypto"],
        },
        {
            "title": "FCA speech on Consumer Duty",
            "link": "https://fca.org.uk/x",
            "published": "2026-05-14T09:00:00+00:00",
            "summary": "Notes on consumer protection.",
            "source": "FCA",
            "primary_tag": "speech",
            "severity": None,
            "topics": [],
        },
    ]
    sub, txt, html = render_daily_digest(
        email="alice@example.com",
        items=items,
        filter_dict={"jurisdiction": "US,UK"},
        unsubscribe_url="https://example.com/api/subscribe/unsubscribe?token=xyz",
        dashboard_url="https://example.com",
    )
    assert "2 items" in sub
    assert "SEC charges firm with fraud" in txt and "SEC charges firm with fraud" in html
    assert "SEVERE" in html
    assert "Unsubscribe" in txt and "Unsubscribe" in html

    # Empty-items path
    sub2, txt2, html2 = render_daily_digest(
        email="alice@example.com", items=[], filter_dict={"tag": "speech"},
        unsubscribe_url="https://example.com/u/x",
    )
    assert "Nothing matched" in txt2 and "Nothing matched" in html2
    print("smoke OK")
