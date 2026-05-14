"""Feature-flag evaluation.

Single entry point ``is_feature_enabled(key, user, subproduct_key=...)`` is
used throughout the app to gate experimental features, run gradual
rollouts, and kill-switch broken features without a deploy.

Lookup order:
  * If ``subproduct_key`` is provided and a row exists for that
    ``(key, subproduct_key)`` pair, evaluate that row.
  * Otherwise fall back to the global row (``subproduct_key IS NULL``).

Per-subproduct rows always override the global value: a subproduct can
opt-in (``enabled_globally=True`` for that row) or kill-switch out
(``enabled_globally=False`` for that row) independently of the global
default. This is the mechanism used to ship features like ``voters_beta``
only to voters.narve.ai, or to flip ``experimental_alerts`` on solely for
crypto.narve.ai subscribers.

Within a chosen row, evaluation order (first match wins):
  1. Flag disabled globally           -> False
  2. User in disabled_for_user_ids    -> False  (explicit exclusion)
  3. User in enabled_for_user_ids     -> True   (explicit inclusion)
  4. User's tier in enabled_for_tiers -> True
  5. rollout_percentage bucket match  -> True   (deterministic per-user)
  6. Otherwise                        -> False

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
    subproduct_key: Optional[str] = None,
    record_event: bool = False,
) -> bool:
    """Resolve a flag for a user, optionally scoped to a subproduct.

    If ``subproduct_key`` is set and a per-subproduct row exists, that row
    overrides the global default. Otherwise the global row is evaluated.
    If neither row exists the flag is treated as missing (False,
    fail-closed). Never raises.
    """
    try:
        row = None
        if subproduct_key:
            row = db.get_feature_flag(flag_key, subproduct_key=subproduct_key)
        if row is None:
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
    # ``subproduct_key`` is a post-migration-183 column; older test fixtures
    # may still hand us a Row without it, so look it up defensively.
    try:
        subp = row["subproduct_key"]
    except (KeyError, IndexError):
        subp = None
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
        "subproduct_key": subp,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by_admin_id": row["updated_by_admin_id"],
    }
