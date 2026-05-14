"""Welcome-email context builder.

The welcome template has three mutually-exclusive variants:

  * ``is_pro_welcome=True``     — user is on the Pro/enterprise bundle; copy
                                   highlights "all 12 dashboards".
  * ``subproduct_name=<str>``    — user has one (or more) specific subproduct
                                   subscriptions; copy is scoped to their
                                   highest-priced flagship sub-brand and
                                   deep-links into that subdomain.
  * ``is_generic_welcome=True``  — fallback for users without an active paid
                                   subscription (e.g. waitlist referral
                                   confirmations).

``build_welcome_context`` centralises this branching so every enqueue site
ends up with the same routing. Imports of ``db`` and ``subproduct`` are
deferred to keep the email_system package import-cycle-safe.
"""

from __future__ import annotations

import logging
from typing import Optional


log = logging.getLogger("email.welcome")


def build_welcome_context(
    user_id: int,
    *,
    display_name: str,
    tier_label_fallback: str = "Free",
) -> dict:
    """Return the template context for the welcome email for ``user_id``.

    Caller is responsible for the ``to`` address (we don't look it up here so
    a test can render without touching the users table). Always returns a
    dict with at minimum ``display_name``, ``tier``, and exactly one of
    ``is_pro_welcome`` / ``subproduct_name`` / ``is_generic_welcome``.

    Defensive: if anything below fails, we fall back to the generic variant
    rather than blocking the welcome email — a bad subscription lookup must
    not deny a user their onboarding email.
    """
    ctx: dict = {
        "display_name": display_name,
        "tier": tier_label_fallback,
    }

    try:
        import db  # local import — avoid email_system <-> db circular
    except Exception:
        log.exception("welcome: db import failed; sending generic")
        ctx["is_generic_welcome"] = True
        return ctx

    # Tier first — Pro plans win regardless of how many subproducts they own.
    try:
        tier = db.get_user_subscription_tier(user_id)
    except Exception:
        log.exception("welcome: tier lookup failed for user_id=%s", user_id)
        tier = "none"

    if tier == "pro":
        ctx["tier"] = "Pro"
        ctx["is_pro_welcome"] = True
        return ctx

    # Otherwise pick a flagship subproduct if any active sub exists.
    try:
        primary = db.get_user_primary_subscription(user_id)
    except Exception:
        log.exception(
            "welcome: primary_subscription lookup failed for user_id=%s", user_id,
        )
        primary = None

    if primary:
        ctx["tier"] = "Trader" if tier == "trader" else (primary.get("display_name") or "Subscriber")
        ctx["subproduct_name"] = primary["display_name"]
        ctx["subproduct_tagline"] = primary.get("tagline") or ""
        # Subdomain landing pages live at <slug>.narve.ai — the slug equals
        # the subdomain by construction (see subproduct.SUBPRODUCTS).
        slug = primary["subdomain"]
        ctx["subproduct_url"] = f"https://{slug}.narve.ai/"
        return ctx

    # No active sub. Trader bundle still gets the generic variant; the
    # "Pro" branch is reserved for the all-dashboards bundle.
    if tier == "trader":
        ctx["tier"] = "Trader"
    else:
        ctx["tier"] = tier_label_fallback
    ctx["is_generic_welcome"] = True
    return ctx


__all__ = ["build_welcome_context"]
