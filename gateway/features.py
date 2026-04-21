"""Feature-flag evaluation.

Single entry point `is_feature_enabled(key, user)` used throughout the app
to gate experimental features, run gradual rollouts, and kill-switch broken
features without a deploy.

Evaluation order (first match wins):
  1. Flag does not exist              → False  (default off; fail-closed)
  2. Flag disabled globally           → False
  3. User in disabled_for_user_ids    → False  (explicit exclusion)
  4. User in enabled_for_user_ids     → True   (explicit inclusion)
  5. User's tier in enabled_for_tiers → True
  6. rollout_percentage bucket match  → True   (deterministic per-user)
  7. Otherwise                        → False

Bucketing is deterministic per (user_id, flag_key) so a given user always
gets the same result for the same flag until the rollout_percentage itself
is bumped. Anonymous callers (user=None) never match rollout.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

import db


log = logging.getLogger("features")


def _parse_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) else list(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _user_tier(user) -> str:
    """Return the subscription tier string used by flag matching.

    Mirrors db.get_user_subscription_tier() but is fault-tolerant: any lookup
    failure just returns "none" so a stuck DB never takes down a page.
    """
    if not user:
        return "anon"
    uid = user.get("user_id") if isinstance(user, dict) else getattr(user, "user_id", None)
    if not uid:
        return "anon"
    try:
        return db.get_user_subscription_tier(uid) or "none"
    except Exception as exc:
        log.warning("feature flag tier lookup failed uid=%s: %s", uid, exc)
        return "none"


def _rollout_bucket(user_id: int, flag_key: str) -> int:
    """Stable 0-99 bucket for a (user, flag) pair."""
    h = hashlib.sha1(f"{flag_key}:{user_id}".encode()).hexdigest()
    return int(h[:8], 16) % 100


def is_feature_enabled(
    flag_key: str,
    user: Optional[dict] = None,
    *,
    record_event: bool = False,
) -> bool:
    """Resolve a flag for a user. Never raises."""
    try:
        row = db.get_feature_flag(flag_key)
    except Exception as exc:
        log.warning("feature flag lookup failed key=%s: %s", flag_key, exc)
        return False

    result = _evaluate(row, user)

    if record_event:
        try:
            uid = user.get("user_id") if user else None
            db.record_feature_flag_event(flag_key, uid, result)
        except Exception:
            pass  # evaluation audit is best-effort

    return result


def _evaluate(row, user: Optional[dict]) -> bool:
    if row is None:
        return False
    if not row["enabled_globally"]:
        return False

    uid = user.get("user_id") if user else None

    disabled_ids = _parse_list(row["disabled_for_user_ids"])
    if uid is not None and uid in disabled_ids:
        return False

    enabled_ids = _parse_list(row["enabled_for_user_ids"])
    if uid is not None and uid in enabled_ids:
        return True

    tiers = _parse_list(row["enabled_for_tiers"])
    if tiers and _user_tier(user) in tiers:
        return True

    rollout = int(row["rollout_percentage"] or 0)
    if rollout > 0 and uid is not None:
        if _rollout_bucket(uid, row["key"]) < rollout:
            return True

    return False


def flag_to_dict(row) -> dict:
    """Serialize a flag row into a plain dict (for admin UI JSON)."""
    if row is None:
        return {}
    return {
        "id": row["id"],
        "key": row["key"],
        "name": row["name"],
        "description": row["description"] or "",
        "enabled_globally": bool(row["enabled_globally"]),
        "enabled_for_tiers": _parse_list(row["enabled_for_tiers"]),
        "enabled_for_user_ids": _parse_list(row["enabled_for_user_ids"]),
        "disabled_for_user_ids": _parse_list(row["disabled_for_user_ids"]),
        "rollout_percentage": int(row["rollout_percentage"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by_admin_id": row["updated_by_admin_id"],
    }
