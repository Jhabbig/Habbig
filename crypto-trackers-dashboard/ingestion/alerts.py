"""User-configurable alerts with webhook delivery.

Stores alert configs as JSON in ./cache/alerts.json. A background loop
checks each alert against current data + fires a webhook POST when
threshold is crossed. Cooldown prevents alert spam (10-min default).

Alert types supported:
  - "price_above"  / "price_below" — CoinGecko universe price
  - "funding_above" / "funding_below" — per-coin median funding rate
  - "fng_above"    / "fng_below"    — Fear & Greed index
  - "btc_fee_above" — BTC fastest sat/vB
  - "eth_gas_above" — ETH fast gwei

Each alert: {id, type, target (coin_id or chain), threshold, webhook_url,
            cooldown_s, last_fired_ts, enabled}

Webhooks receive JSON: {alert, observed_value, threshold, fired_at_iso, message}
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ingestion import _cache, binance, coingecko, etherscan_gas, fear_greed, mempool_btc

log = logging.getLogger("ct.alerts")
_LOCK = threading.Lock()

ALERT_TYPES = (
    "price_above", "price_below",
    "funding_above", "funding_below",
    "fng_above", "fng_below",
    "btc_fee_above", "eth_gas_above",
)
DEFAULT_COOLDOWN_S = 600  # 10 min


def _store_path() -> Path:
    base = os.environ.get("CT_CACHE_DIR")
    if base:
        return Path(base) / "alerts.json"
    return Path(__file__).resolve().parent.parent / "cache" / "alerts.json"


def _load_store() -> list[dict]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Failed to load alerts store: %s", e)
        return []


def _save_store(alerts: list[dict]) -> None:
    p = _store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Failed to create alerts dir: %s", e)
        return
    try:
        with _LOCK, tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(p.parent),
            prefix=p.name + ".", suffix=".tmp", delete=False,
        ) as fh:
            json.dump(alerts, fh, indent=2)
            tmp = fh.name
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Failed to persist alerts: %s", e)


def list_alerts() -> list[dict]:
    return _load_store()


def create_alert(alert_type: str, target: str, threshold: float,
                  webhook_url: Optional[str] = None,
                  cooldown_s: int = DEFAULT_COOLDOWN_S,
                  label: Optional[str] = None) -> dict:
    if alert_type not in ALERT_TYPES:
        return {"error": f"unknown alert_type {alert_type}",
                "supported": list(ALERT_TYPES)}
    alerts = _load_store()
    alert = {
        "id": str(uuid.uuid4())[:8],
        "type": alert_type,
        "target": target,
        "threshold": float(threshold),
        "webhook_url": webhook_url,
        "cooldown_s": int(cooldown_s),
        "last_fired_ts": 0.0,
        "fire_count": 0,
        "enabled": True,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    alerts.append(alert)
    _save_store(alerts)
    return alert


def delete_alert(alert_id: str) -> bool:
    alerts = _load_store()
    before = len(alerts)
    alerts = [a for a in alerts if a.get("id") != alert_id]
    if len(alerts) == before:
        return False
    _save_store(alerts)
    return True


def toggle_alert(alert_id: str, enabled: bool) -> bool:
    alerts = _load_store()
    found = False
    for a in alerts:
        if a.get("id") == alert_id:
            a["enabled"] = bool(enabled)
            found = True
            break
    if found:
        _save_store(alerts)
    return found


def _observe(alert: dict) -> Optional[float]:
    """Look up the current observed value for an alert's target."""
    t = alert.get("type", "")
    target = alert.get("target", "")
    if t in ("price_above", "price_below"):
        univ = coingecko.universe(500)
        for c in univ.get("coins") or []:
            if (c.get("id") == target or c.get("symbol", "").lower() == target.lower()):
                return c.get("current_price")
        return None
    if t in ("funding_above", "funding_below"):
        prem = binance.futures_premium_index()
        if prem.get("error"):
            return None
        sym = target.upper() + "USDT" if not target.upper().endswith("USDT") else target.upper()
        for r in prem.get("rows") or []:
            if r.get("symbol") == sym:
                return r.get("funding_rate")
        return None
    if t in ("fng_above", "fng_below"):
        fng = fear_greed.index(2)
        latest = fng.get("latest") or {}
        return latest.get("value")
    if t == "btc_fee_above":
        net = mempool_btc.network_status()
        fees = net.get("fees_sat_per_vb") or {}
        return fees.get("fastest")
    if t == "eth_gas_above":
        gas = etherscan_gas.eth_gas_oracle()
        if gas.get("error"):
            return None
        return gas.get("fast_gwei")
    return None


def _is_triggered(alert: dict, observed: float) -> bool:
    t = alert.get("type", "")
    thr = alert.get("threshold", 0)
    if t.endswith("_above"):
        return observed > thr
    if t.endswith("_below"):
        return observed < thr
    return False


def _detect_webhook_kind(url: str) -> str:
    """Infer webhook flavour from the host so we can format the payload."""
    if not url:
        return "generic"
    u = url.lower()
    if "hooks.slack.com" in u:
        return "slack"
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    return "generic"


def _format_for_webhook(kind: str, alert: dict, observed: float,
                        message: str) -> dict:
    """Shape the payload for the destination webhook flavour."""
    if kind == "slack":
        return {
            "text": f":rotating_light: *Narve alert*: {message}",
            "attachments": [{
                "color": "#f59e0b",
                "fields": [
                    {"title": "Type", "value": alert.get("type"), "short": True},
                    {"title": "Target", "value": str(alert.get("target") or "—"), "short": True},
                    {"title": "Threshold", "value": str(alert.get("threshold")), "short": True},
                    {"title": "Observed", "value": str(observed), "short": True},
                ],
                "footer": "Crypto Trackers · narve.ai",
                "ts": int(datetime.now(timezone.utc).timestamp()),
            }],
        }
    if kind == "discord":
        return {
            "username": "Narve Crypto Trackers",
            "embeds": [{
                "title": "Alert triggered",
                "description": message,
                "color": 0xf59e0b,
                "fields": [
                    {"name": "Type", "value": str(alert.get("type")), "inline": True},
                    {"name": "Target", "value": str(alert.get("target") or "—"), "inline": True},
                    {"name": "Threshold", "value": str(alert.get("threshold")), "inline": True},
                    {"name": "Observed", "value": str(observed), "inline": True},
                ],
                "footer": {"text": "Crypto Trackers · narve.ai"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
        }
    # Generic JSON
    return {
        "alert": alert,
        "observed_value": observed,
        "threshold": alert.get("threshold"),
        "fired_at_iso": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }


def _fire(alert: dict, observed: float) -> bool:
    """POST to webhook in the right format for Slack / Discord / generic.

    Returns True on success."""
    message = _format_message(alert, observed)
    webhook = alert.get("webhook_url")
    if not webhook:
        return True
    kind = _detect_webhook_kind(webhook)
    payload = _format_for_webhook(kind, alert, observed, message)
    try:
        requests.post(webhook, json=payload, timeout=8,
                      headers={"User-Agent": "narve-crypto-trackers/1.0"})
    except requests.RequestException as e:
        log.warning("alert %s webhook failed: %s", alert.get("id"), e)
        return False
    return True


def _format_message(alert: dict, observed: float) -> str:
    t = alert.get("type", "")
    target = alert.get("target", "")
    thr = alert.get("threshold", 0)
    if t.startswith("price"):
        op = "above" if t.endswith("above") else "below"
        return f"{target.upper()} price ${observed:,.4f} crossed {op} ${thr:,.4f}"
    if t.startswith("funding"):
        op = "above" if t.endswith("above") else "below"
        return f"{target.upper()} funding rate {observed*100:.4f}% (8h) crossed {op} {thr*100:.4f}%"
    if t.startswith("fng"):
        op = "above" if t.endswith("above") else "below"
        return f"Fear & Greed Index {observed:.0f} crossed {op} {thr:.0f}"
    if t == "btc_fee_above":
        return f"BTC fastest fee {observed:.0f} sat/vB crossed above {thr:.0f}"
    if t == "eth_gas_above":
        return f"ETH gas fast {observed:.1f} gwei crossed above {thr:.1f}"
    return f"alert triggered: observed={observed} threshold={thr}"


def check_all() -> list[dict]:
    """Walk every enabled alert; fire those that cross their threshold and
    are past their cooldown. Returns the list of fired alerts (for the
    /api/alerts/check route)."""
    alerts = _load_store()
    fired: list[dict] = []
    now = time.time()
    for a in alerts:
        if not a.get("enabled"):
            continue
        if (now - a.get("last_fired_ts", 0)) < a.get("cooldown_s", DEFAULT_COOLDOWN_S):
            continue
        observed = _observe(a)
        if observed is None:
            continue
        if not _is_triggered(a, observed):
            continue
        ok = _fire(a, observed)
        a["last_fired_ts"] = now
        a["fire_count"] = a.get("fire_count", 0) + 1
        a["last_observed_value"] = observed
        a["last_fire_ok"] = ok
        fired.append({**a, "observed_value": observed})
    if fired:
        _save_store(alerts)
    return fired


def _cache_summary() -> dict:
    """Lightweight rollup of alert state for /api/alerts."""
    alerts = _load_store()
    return {
        "count": len(alerts),
        "enabled": sum(1 for a in alerts if a.get("enabled")),
        "alerts": alerts,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def summary() -> dict:
    # Don't cache - alerts list is fast (local file) and changes via CRUD
    # need to be visible immediately to the UI.
    return _cache_summary()
