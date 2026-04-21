"""Private referral reward tiering.

The product rule, copied verbatim from the brief:

  Refer 1 paid user   → 1 month free (any tier)
  Refer 5 paid users  → upgrade to next tier for 1 month
  Refer 10 paid users → Pro for free for 3 months
  Rewards stack.

"Stack" in practice means: each newly-converted referral earns its own
reward based on how many converted referrals the user has TOTAL at the
moment the job runs. The 1st converted earns "1 month free" (applied as
a gifted_subscription to their current tier). The 5th converted additionally
earns "1 month tier upgrade". The 10th earns "3 months free Pro". A user
who converts 10 in one day would get three separate reward grants — one for
crossing count=1, one for count=5, one for count=10 — recorded as three
rows in the referrals table (the three most recently converted, matched
in asc order).

This module is deliberately framework-agnostic: no FastAPI, no email, no
Stripe. It returns dicts the reward job turns into gifted_subscriptions
rows + email enqueues. That way the tiering logic is trivially unit-testable.
"""

from __future__ import annotations

from typing import Optional


# Tier hierarchy for the "next tier for 1 month" reward at count=5. Kept
# here rather than in db.py so the reward table stays next to the logic
# that consumes it. If PLAN_DEFS in server.py ever grows another tier
# between trader and pro, update here too.
_TIER_ORDER = ("none", "trader", "pro")


def next_tier_above(current_tier: str) -> str:
    """The tier one step above the user's current one. 'pro' has no next
    tier — we just give them another month at pro, which is the best
    available."""
    current = current_tier or "none"
    if current not in _TIER_ORDER:
        current = "none"
    idx = _TIER_ORDER.index(current)
    if idx + 1 < len(_TIER_ORDER):
        return _TIER_ORDER[idx + 1]
    return "pro"  # already at the top; stay there


# Rewards keyed on the total converted count at the moment of grant.
# None means "no additional reward triggered by this count". The job
# looks up the reward for each newly-granted referral by mapping it
# to the user's running count of already-rewarded referrals + 1.
_REWARDS_BY_COUNT: dict[int, Optional[dict]] = {
    1:  {"type": "one_month_free",  "months": 1, "tier_mode": "current"},
    5:  {"type": "tier_upgrade",    "months": 1, "tier_mode": "next"},
    10: {"type": "pro_three_months", "months": 3, "tier_mode": "pro"},
}


def reward_for_conversion_number(conversion_number: int) -> Optional[dict]:
    """Which reward does the *N*th converted referral unlock, if any?

    Returns {"type", "months", "tier_mode"} or None. `tier_mode` is one of:
      - "current": gift the user's current tier
      - "next":    gift the tier immediately above their current tier
      - "pro":     gift pro outright regardless of current tier

    Callers resolve `tier_mode` → concrete tier at grant-time using the
    user's subscription state (via db.get_user_subscription_tier()) so a
    user who has upgraded between conversion and payout gets the reward
    at the tier they hold *at grant time*, not their tier when they
    referred someone six months ago.
    """
    return _REWARDS_BY_COUNT.get(conversion_number)


def resolve_tier_mode(tier_mode: str, current_tier: str) -> str:
    """Turn the abstract reward tier_mode into a concrete tier name."""
    if tier_mode == "pro":
        return "pro"
    if tier_mode == "next":
        return next_tier_above(current_tier)
    # "current" falls through — but if current_tier is 'none' the user
    # isn't actively paying anymore and the reward-granting job should
    # have skipped them upstream. Default to trader as a safe floor
    # rather than inserting a 'none' gifted subscription row.
    if current_tier == "none" or not current_tier:
        return "trader"
    return current_tier


def compute_reward_for_referral(
    *,
    total_converted_before_this_one: int,
    current_tier: str,
) -> Optional[dict]:
    """Entry point used by the reward job. Takes the number of already-
    rewarded referrals the user has BEFORE this one gets processed, adds 1
    (this one), looks up the reward, and resolves the tier. Returns the
    fully-concrete reward dict the job should write to gifted_subscriptions,
    or None if this particular conversion number doesn't trigger a reward.

    Example:
      total_converted_before_this_one=0, current_tier='trader'
        → {type: 'one_month_free', months: 1, tier: 'trader'}
      total_converted_before_this_one=4, current_tier='trader'
        → {type: 'tier_upgrade', months: 1, tier: 'pro'}
      total_converted_before_this_one=9, current_tier='pro'
        → {type: 'pro_three_months', months: 3, tier: 'pro'}
      total_converted_before_this_one=1, current_tier='trader'
        → None (the 2nd conversion doesn't trigger anything)
    """
    conversion_number = total_converted_before_this_one + 1
    spec = reward_for_conversion_number(conversion_number)
    if spec is None:
        return None
    tier = resolve_tier_mode(spec["tier_mode"], current_tier)
    return {
        "type": spec["type"],
        "months": spec["months"],
        "tier": tier,
        "conversion_number": conversion_number,
    }


def progress_toward_next_reward(total_converted: int) -> dict:
    """For the /settings/referrals progress bar. Returns the thresholds
    used to render the '4 of 5 — unlock tier upgrade' progress state.

    Output shape:
      {
        "current": int,          # how many conversions they have now
        "next_milestone": int,   # 1, 5, 10, or None (if past the top)
        "next_reward_label": str,# "1 month free" / "tier upgrade" / etc.
        "remaining": int,        # conversions needed to hit next milestone
      }
    """
    milestones = sorted(_REWARDS_BY_COUNT.keys())
    for m in milestones:
        if total_converted < m:
            spec = _REWARDS_BY_COUNT[m]
            label = {
                "one_month_free":    "1 month free",
                "tier_upgrade":      "tier upgrade for 1 month",
                "pro_three_months":  "Pro for 3 months",
            }.get(spec["type"], spec["type"])
            return {
                "current": total_converted,
                "next_milestone": m,
                "next_reward_label": label,
                "remaining": m - total_converted,
            }
    return {
        "current": total_converted,
        "next_milestone": None,
        "next_reward_label": "You've earned every milestone. Thank you!",
        "remaining": 0,
    }


def format_reward_label(reward_type: str, months: int, tier: str) -> str:
    """Human-readable label for a granted reward — used on the referrals
    page under 'Rewards' and inside the congratulations email."""
    if reward_type == "one_month_free":
        return f"1 month of {tier.title()} free"
    if reward_type == "tier_upgrade":
        return f"1 month upgrade to {tier.title()}"
    if reward_type == "pro_three_months":
        return "3 months of Pro free"
    return f"{months} month(s) of {tier.title()}"
