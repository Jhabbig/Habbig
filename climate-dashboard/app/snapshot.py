"""Plain-text snapshot of the dashboard's current state.

Built so users (or cron jobs) can pull a single text blob to post to
Twitter / Bluesky / email / RSS without having to render the full
dashboard. Pure-derivative of the cached upstream data.

Also exposes an RSS feed generator over the highlights chips so users
can subscribe to "today's climate" in their feed reader.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone
from typing import Optional


def text_snapshot(*, gistemp=None, co2=None, methane=None, n2o=None, sf6=None,
                  sea_ice=None, oni=None, forcing=None, highlights=None,
                  emissions=None) -> str:
    """Build a one-page plain-text dashboard summary."""
    lines = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines.append(f"Climate snapshot — {today}")
    lines.append("=" * 50)

    if highlights:
        lines.append("")
        lines.append("HIGHLIGHTS")
        for h in highlights[:6]:
            lines.append(f"  • {h.get('text', '')}")

    if gistemp and gistemp.get("annual"):
        latest_ann = gistemp["annual"][-1]
        lines.append("")
        lines.append("TEMPERATURE (NASA GISTEMP, vs 1951-1980 baseline)")
        lines.append(f"  Last full year: {latest_ann['year']} → +{latest_ann['anomaly_c']:.2f}°C")

    if co2 and co2.get("latest"):
        lines.append("")
        lines.append("ATMOSPHERIC GASES (latest monthly mean)")
        lines.append(f"  CO₂   {co2['latest']['ppm']:.2f} ppm  ({co2['latest']['year']}-{co2['latest']['month']:02d}, NOAA Mauna Loa)")
        if methane and methane.get("latest"):
            lines.append(f"  CH₄   {methane['latest']['ppb']:.1f} ppb")
        if n2o and n2o.get("latest"):
            lines.append(f"  N₂O   {n2o['latest']['ppb']:.2f} ppb")
        if sf6 and sf6.get("latest"):
            lines.append(f"  SF₆   {sf6['latest']['ppt']:.2f} ppt")

    if forcing and forcing.get("total_wm2") is not None:
        lines.append("")
        lines.append("RADIATIVE FORCING (IPCC AR5 / Myhre)")
        lines.append(f"  Total: {forcing['total_wm2']:.2f} W/m² above pre-industrial")
        lines.append(f"  Effective CO₂: {forcing['effective_co2_ppm']:.0f} ppm")

    if sea_ice and sea_ice.get("arctic"):
        from .models.sea_ice import daily_record_check
        rec = daily_record_check(sea_ice)
        if rec:
            lines.append("")
            lines.append("ARCTIC SEA ICE")
            lines.append(f"  {rec['date']}: {rec['extent_mkm2']:.2f} Mkm²")
            lines.append(f"  Rank: #{rec['rank_lowest_in_record']} lowest of {rec['history_years']} on this day-of-year")

    if oni and oni.get("state"):
        lines.append("")
        lines.append("ENSO REGIME")
        lines.append(f"  {oni['state']} — ONI {oni['latest']['oni']:+.2f} ({oni['latest']['year']}-{oni['latest']['month']:02d})")

    if emissions and emissions.get("top_emitters"):
        lines.append("")
        lines.append(f"TOP CO₂ EMITTERS ({emissions.get('latest_year')}, OWID)")
        for i, e in enumerate(emissions["top_emitters"][:5], start=1):
            lines.append(f"  {i}. {e['country']:<22} {e['co2_mt']/1000:.2f} Gt  ({e['share_global']:.1f}% global)")

    lines.append("")
    lines.append("More: https://climate.narve.ai · methodology: /methodology")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return html_lib.escape(s, quote=True)


def rss_feed(highlights: list[dict], *, base_url: str = "https://climate.narve.ai") -> str:
    """RSS 2.0 feed of the highlights chips so users can subscribe in a feed
    reader. Each chip is one item; pubDate is the response time (intra-day
    refreshes will publish new items)."""
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = []
    for i, h in enumerate(highlights or []):
        title = h.get("text", "")
        kind = h.get("kind", "highlight")
        items.append(f"""    <item>
      <title>{_esc(title)}</title>
      <description>{_esc(f'[{kind}] {title}')}</description>
      <guid isPermaLink="false">{_esc(base_url)}/highlights/{_esc(now)}/{i}</guid>
      <pubDate>{now}</pubDate>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Climate Dashboard — Today's Highlights</title>
    <link>{_esc(base_url)}</link>
    <description>Auto-derived climate findings, updated as upstream data refreshes.</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>"""
