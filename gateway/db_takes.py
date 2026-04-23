"""Community Takes DB layer.

Separate file so the 3000+-line db.py doesn't bloat further and so tests
can mock this surface in isolation. Every public function opens its own
`db.conn()` context — no caller is expected to manage transactions.

Naming convention matches db.py: plain functions, sqlite3.Row returns,
raises nothing for "not found" (returns None / empty list). Validation
errors raise ValueError with a short human-readable message.

Quality score formula (compute_quality_score):

    net = upvotes - downvotes
    base = net * (0.5 + 0.5 * author_cred)   # half baseline + half amplified by cred
    if resolved_correct is set:
        base *= 1.2 if correct else 0.7      # correctness bonus / penalty

Author credibility comes from `user_accuracy.accuracy_score` (migration 023).
Users with no accuracy history fall back to 0.5 (neutral) — this prevents
brand-new accounts from either inflating OR deflating their first take.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Optional

import db


# ── Limits + constants ──────────────────────────────────────────────────────

REASONING_MIN_CHARS = 50
REASONING_MAX_CHARS = 2000
EDIT_WINDOW_SECONDS = 24 * 60 * 60  # 24h
MAX_TAKES_PER_DAY = 10

SHADOW_HIDE_DOWNVOTES = 3
SHADOW_HIDE_QUALITY = -5.0

VALID_POSITIONS = ("yes", "no", "neutral")

# Default author credibility when no user_accuracy row exists yet.
DEFAULT_AUTHOR_CRED = 0.5


# ── Validation helpers ──────────────────────────────────────────────────────

_MARKDOWN_RE = re.compile(
    r"!\[[^\]]*\]\([^\)]*\)"      # images
    r"|\[([^\]]+)\]\([^\)]*\)"    # links → keep visible text
    r"|[*_~`>#]{1,3}"             # markdown punctuation
    r"|^\s*[-*+]\s+"              # bullet markers at line start
)


def _strip_markdown(text: str) -> str:
    """Remove markdown syntax but keep link text + line breaks.

    Not cryptographically tight — the XSS guard lives in the template
    layer (`html.escape`). This is purely UX: we don't want people to
    hide `<script>` inside `[x](javascript:...)` or pad a take with
    asterisks to look like it says something when it doesn't.
    """
    if not text:
        return ""
    # Replace `[visible](url)` → `visible`, strip trailing images/bullets/etc.
    def _link_keep_text(m: re.Match) -> str:
        return m.group(1) or ""
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", text)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[*_~]{1,3}", "", cleaned)
    cleaned = re.sub(r"(^|\n)\s{0,3}#{1,6}\s+", r"\1", cleaned)
    cleaned = re.sub(r"(^|\n)\s*[-*+]\s+", r"\1• ", cleaned)
    cleaned = re.sub(r"(^|\n)\s*>\s?", r"\1", cleaned)
    # Collapse ≥3 blank lines to 2, and strip every row of leading/trailing WS.
    lines = [ln.rstrip() for ln in cleaned.split("\n")]
    collapsed: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks <= 1:
                collapsed.append("")
        else:
            blanks = 0
            collapsed.append(ln.lstrip())
    return "\n".join(collapsed).strip()


def _normalize_position(pos: str) -> str:
    if not pos:
        raise ValueError("position required")
    p = pos.strip().lower()
    if p not in VALID_POSITIONS:
        raise ValueError(f"position must be one of {VALID_POSITIONS}")
    return p


def _validate_confidence(conf: Optional[int]) -> Optional[int]:
    if conf is None:
        return None
    try:
        n = int(conf)
    except (TypeError, ValueError) as e:
        raise ValueError("confidence must be an integer") from e
    if not (1 <= n <= 10):
        raise ValueError("confidence must be in 1..10")
    return n


def _validate_reasoning(text: str) -> str:
    cleaned = _strip_markdown(text or "")
    if len(cleaned) < REASONING_MIN_CHARS:
        raise ValueError(
            f"reasoning must be at least {REASONING_MIN_CHARS} characters"
        )
    if len(cleaned) > REASONING_MAX_CHARS:
        raise ValueError(
            f"reasoning must be at most {REASONING_MAX_CHARS} characters"
        )
    return cleaned


# ── Credibility lookup ──────────────────────────────────────────────────────


def get_user_credibility(user_id: int) -> float:
    """Return the user's accuracy_score from user_accuracy, or 0.5 default.

    Clamps to [0.0, 1.0]. Never raises — a missing table (fresh DB, no
    leaderboard migration yet) falls back to the default.
    """
    if user_id is None:
        return DEFAULT_AUTHOR_CRED
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT accuracy_score FROM user_accuracy WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return DEFAULT_AUTHOR_CRED
    if row is None or row["accuracy_score"] is None:
        return DEFAULT_AUTHOR_CRED
    try:
        score = float(row["accuracy_score"])
    except (TypeError, ValueError):
        return DEFAULT_AUTHOR_CRED
    return max(0.0, min(1.0, score))


def get_user_take_accuracy(user_id: int) -> Optional[float]:
    """Fraction of this user's resolved takes that were correct.

    Returns None if the user has no scored takes yet (neutral takes never
    resolve, so they don't count in the denominator). Range [0.0, 1.0].

    This is deliberately derived at query time from `market_takes` rather
    than stored in `user_accuracy.accuracy_score`, so the "small credibility
    nudge" for correct takes stays SEPARATE from the global credibility
    score that feeds into the leaderboard / quality-score formula.
    """
    if not user_id:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT "
            "  SUM(CASE WHEN resolved_correct = 1 THEN 1 ELSE 0 END) AS correct, "
            "  SUM(CASE WHEN resolved_correct = 0 THEN 1 ELSE 0 END) AS wrong "
            "FROM market_takes WHERE user_id = ? AND is_deleted = 0",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    c_n = int(row["correct"] or 0)
    w_n = int(row["wrong"] or 0)
    denom = c_n + w_n
    if denom == 0:
        return None
    return c_n / float(denom)


def get_blended_credibility(user_id: int) -> float:
    """Global accuracy + a small nudge from correct takes.

    Formula:
        blended = 0.85 * global_accuracy + 0.15 * take_accuracy

    Applied only once the user has resolved takes; with zero scored takes
    the blend falls back to the plain global credibility so the nudge can
    never inflate (or deflate) a brand-new poster.

    The small 0.15 weight keeps this as a genuine *nudge* — someone with a
    perfect take record (accuracy 1.0) gets at most +0.075 above their
    global score, which matters for display + quality_score but doesn't
    overwhelm the predictions engine's signal.
    """
    base = get_user_credibility(user_id)
    take_acc = get_user_take_accuracy(user_id)
    if take_acc is None:
        return base
    blended = 0.85 * base + 0.15 * take_acc
    return max(0.0, min(1.0, blended))


def user_opts_in_public_takes(user_id: int) -> bool:
    """True if this user has opted into the public leaderboard.

    Reuses the existing `users.leaderboard_participation` flag — a user who
    agreed to have their predictions appear on the public leaderboard has
    already consented to a public identity on the site. No new opt-in is
    needed just for takes.
    """
    if not user_id:
        return False
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT leaderboard_participation FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not row:
        return False
    try:
        return bool(int(row["leaderboard_participation"] or 0))
    except (TypeError, ValueError):
        return False


def list_user_best_takes(user_id: int, *, limit: int = 5) -> list[sqlite3.Row]:
    """Top-scoring visible takes by this user. Used on the public profile.

    Filter rules (stricter than the per-market lists, because the profile
    curates):
      - is_deleted = 0
      - shadow_hidden = 0 (never leak an author-only hidden take off-market)
      - quality_score IS NOT NULL (skip brand-new takes with no score)

    Ordered by quality_score DESC, created_at DESC as tiebreak.
    """
    if not user_id:
        return []
    with db.conn() as c:
        return list(c.execute(
            "SELECT * FROM market_takes "
            "WHERE user_id = ? AND is_deleted = 0 AND shadow_hidden = 0 "
            "      AND quality_score IS NOT NULL "
            "ORDER BY quality_score DESC, created_at DESC LIMIT ?",
            (user_id, max(1, int(limit))),
        ).fetchall())


# ── Quality score ──────────────────────────────────────────────────────────


def compute_quality_score(take: Any, *, author_cred: Optional[float] = None) -> float:
    """Compute a single take's quality score.

    Accepts either a sqlite3.Row / dict-like with the necessary columns,
    or a plain dict. `author_cred` may be pre-fetched (for batch recompute)
    to avoid per-row user_accuracy lookups.
    """
    upvotes = int(take["upvotes"] or 0)
    downvotes = int(take["downvotes"] or 0)
    net_votes = upvotes - downvotes

    if author_cred is None:
        author_cred = get_user_credibility(take["user_id"])
    author_cred = max(0.0, min(1.0, float(author_cred)))

    score = net_votes * (0.5 + 0.5 * author_cred)

    correctness = take["resolved_correct"] if "resolved_correct" in take.keys() else None
    if correctness is not None:
        score *= 1.2 if int(correctness) == 1 else 0.7

    return float(score)


# ── Posting rate limit ─────────────────────────────────────────────────────


def count_takes_today(user_id: int, *, now: Optional[int] = None) -> int:
    """Return how many takes this user has posted in the last 24 hours."""
    if now is None:
        now = int(time.time())
    cutoff = now - 86400
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM market_takes "
            "WHERE user_id = ? AND created_at >= ? AND is_deleted = 0",
            (user_id, cutoff),
        ).fetchone()
    return int(row["n"] if row else 0)


# ── Core CRUD ──────────────────────────────────────────────────────────────


def create_take(
    *,
    user_id: int,
    market_slug: str,
    position: str,
    reasoning: str,
    confidence: Optional[int] = None,
) -> int:
    """Insert a new take. Enforces validation + rate limit + uniqueness.

    Raises ValueError on bad input, rate limit, or duplicate.
    Returns the new take id.
    """
    if not user_id:
        raise ValueError("user_id required")
    slug = (market_slug or "").strip()
    if not slug:
        raise ValueError("market_slug required")
    pos = _normalize_position(position)
    conf = _validate_confidence(confidence)
    clean_reasoning = _validate_reasoning(reasoning)

    if count_takes_today(user_id) >= MAX_TAKES_PER_DAY:
        raise ValueError(
            f"rate limit: {MAX_TAKES_PER_DAY} takes per 24 hours"
        )

    now = int(time.time())
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO market_takes "
                "(user_id, market_slug, position, confidence, reasoning, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, slug, pos, conf, clean_reasoning, now),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as e:
        # uq_takes_user_market — one live take per (user, market).
        if "uq_takes_user_market" in str(e) or "UNIQUE" in str(e).upper():
            raise ValueError("you already have a take on this market") from e
        raise


def get_take(take_id: int, *, include_deleted: bool = False) -> Optional[sqlite3.Row]:
    """Fetch a single take by id. Returns None on not-found."""
    with db.conn() as c:
        if include_deleted:
            return c.execute(
                "SELECT * FROM market_takes WHERE id = ?", (take_id,)
            ).fetchone()
        return c.execute(
            "SELECT * FROM market_takes WHERE id = ? AND is_deleted = 0",
            (take_id,),
        ).fetchone()


def list_market_takes(
    market_slug: str,
    *,
    viewer_user_id: Optional[int] = None,
    position_filter: Optional[str] = None,
    sort: str = "quality",
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List takes for a market. Shadow-hidden takes are filtered out unless
    the viewer is the author.

    sort ∈ {"quality", "newest", "votes"}. Unknown values fall back to quality.
    """
    slug = (market_slug or "").strip()
    if not slug:
        return []

    where = ["market_slug = ?", "is_deleted = 0"]
    params: list[Any] = [slug]

    # Shadow hide: visible to everyone EXCEPT the author.
    if viewer_user_id is None:
        where.append("shadow_hidden = 0")
    else:
        where.append("(shadow_hidden = 0 OR user_id = ?)")
        params.append(viewer_user_id)

    if position_filter:
        pos = position_filter.strip().lower()
        if pos in VALID_POSITIONS:
            where.append("position = ?")
            params.append(pos)

    order = {
        "newest": "created_at DESC",
        "votes": "(upvotes - downvotes) DESC, created_at DESC",
        "quality": "COALESCE(quality_score, 0) DESC, created_at DESC",
    }.get(sort, "COALESCE(quality_score, 0) DESC, created_at DESC")

    sql = (
        f"SELECT * FROM market_takes WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ? OFFSET ?"
    )
    params.extend([max(1, int(limit)), max(0, int(offset))])
    with db.conn() as c:
        return list(c.execute(sql, params).fetchall())


def count_market_takes(
    market_slug: str,
    *,
    viewer_user_id: Optional[int] = None,
    position_filter: Optional[str] = None,
) -> int:
    """Return the count of visible takes on this market (no limit/offset)."""
    slug = (market_slug or "").strip()
    if not slug:
        return 0
    where = ["market_slug = ?", "is_deleted = 0"]
    params: list[Any] = [slug]
    if viewer_user_id is None:
        where.append("shadow_hidden = 0")
    else:
        where.append("(shadow_hidden = 0 OR user_id = ?)")
        params.append(viewer_user_id)
    if position_filter and position_filter.strip().lower() in VALID_POSITIONS:
        where.append("position = ?")
        params.append(position_filter.strip().lower())
    sql = f"SELECT COUNT(*) AS n FROM market_takes WHERE {' AND '.join(where)}"
    with db.conn() as c:
        row = c.execute(sql, params).fetchone()
    return int(row["n"] if row else 0)


def list_user_takes(
    user_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
    include_hidden: bool = True,
) -> list[sqlite3.Row]:
    """Return this user's own take history — newest first."""
    with db.conn() as c:
        where = "user_id = ? AND is_deleted = 0"
        if not include_hidden:
            where += " AND shadow_hidden = 0"
        return list(c.execute(
            f"SELECT * FROM market_takes WHERE {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, max(1, int(limit)), max(0, int(offset))),
        ).fetchall())


def can_edit(take: Any, *, now: Optional[int] = None) -> bool:
    """Return True if the take is still within its 24h edit window."""
    if not take:
        return False
    if now is None:
        now = int(time.time())
    return (now - int(take["created_at"] or 0)) <= EDIT_WINDOW_SECONDS


def update_take(
    take_id: int,
    user_id: int,
    *,
    position: Optional[str] = None,
    confidence: Optional[int] = None,
    reasoning: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Edit a take within its 24h window. Owner-only. Returns (ok, error_msg).

    Any of position/confidence/reasoning may be omitted — only provided
    fields are updated. Validation is re-applied to provided fields.
    """
    row = get_take(take_id)
    if not row:
        return False, "take not found"
    if row["user_id"] != user_id:
        return False, "not the owner"
    if not can_edit(row):
        return False, "edit window (24h) has expired"

    sets: list[str] = []
    params: list[Any] = []
    if position is not None:
        sets.append("position = ?")
        params.append(_normalize_position(position))
    if confidence is not None or confidence == 0:
        sets.append("confidence = ?")
        params.append(_validate_confidence(confidence))
    if reasoning is not None:
        sets.append("reasoning = ?")
        params.append(_validate_reasoning(reasoning))
    if not sets:
        return False, "no fields to update"

    sets.append("edited_at = ?")
    params.append(int(time.time()))
    params.extend([take_id, user_id])

    with db.conn() as c:
        c.execute(
            f"UPDATE market_takes SET {', '.join(sets)} "
            "WHERE id = ? AND user_id = ? AND is_deleted = 0",
            params,
        )
    return True, None


def delete_take(take_id: int, user_id: int) -> bool:
    """Owner soft-deletes their take. Returns True on success."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE market_takes SET is_deleted = 1 "
            "WHERE id = ? AND user_id = ? AND is_deleted = 0",
            (take_id, user_id),
        )
        return cur.rowcount > 0


def admin_delete_take(take_id: int) -> bool:
    """Admin hard-deletes (soft-deletes in schema — row kept for audit)."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE market_takes SET is_deleted = 1 WHERE id = ?",
            (take_id,),
        )
        return cur.rowcount > 0


# ── Voting ──────────────────────────────────────────────────────────────────


def _recount_votes(c, take_id: int) -> tuple[int, int]:
    row = c.execute(
        "SELECT "
        "  SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END) AS up, "
        "  SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) AS dn "
        "FROM take_votes WHERE take_id = ?",
        (take_id,),
    ).fetchone()
    up = int(row["up"] or 0) if row else 0
    dn = int(row["dn"] or 0) if row else 0
    return up, dn


def _apply_vote_effects(c, take_id: int) -> None:
    """Refresh upvotes/downvotes + quality_score + shadow_hidden on one take.

    When a take transitions INTO shadow_hidden state (prev=0, new=1), the
    author gets an in-app notification via `notifications` (migration 026)
    so they can see why their take vanished from other users' feeds.
    The notify helper is a best-effort fire-and-forget — a missing
    notifications table or an older gateway build just logs and skips.
    """
    # Read the take's PREVIOUS shadow_hidden + market_slug BEFORE updating
    # — we need the previous value to detect the 0→1 transition, and the
    # slug for the notification deep-link.
    prev = c.execute(
        "SELECT shadow_hidden, market_slug FROM market_takes WHERE id = ?",
        (take_id,),
    ).fetchone()
    prev_shadow = int(prev["shadow_hidden"] or 0) if prev else 0
    market_slug = prev["market_slug"] if prev else ""

    up, dn = _recount_votes(c, take_id)
    c.execute(
        "UPDATE market_takes SET upvotes = ?, downvotes = ? WHERE id = ?",
        (up, dn, take_id),
    )
    take = c.execute(
        "SELECT id, user_id, upvotes, downvotes, resolved_correct "
        "FROM market_takes WHERE id = ?",
        (take_id,),
    ).fetchone()
    if take is None:
        return
    q = compute_quality_score(take)
    # Shadow-hide: only when BOTH conditions fire (3+ downvotes AND
    # quality < -5). That way a highly-upvoted take with a few downvotes
    # doesn't vanish, and a single grudge-voter can't bury anyone.
    shadow = 1 if (dn >= SHADOW_HIDE_DOWNVOTES and q < SHADOW_HIDE_QUALITY) else 0
    c.execute(
        "UPDATE market_takes SET quality_score = ?, shadow_hidden = ? WHERE id = ?",
        (q, shadow, take_id),
    )

    # Edge-triggered: only notify on the first transition 0→1, not on every
    # subsequent vote while still hidden (which would spam the author).
    if prev_shadow == 0 and shadow == 1:
        _notify_shadow_hidden(c, take["user_id"], take_id, market_slug)


def _notify_shadow_hidden(c, user_id: int, take_id: int, market_slug: str) -> None:
    """Insert a row into `notifications` telling the author their take was
    shadow-hidden. Never raises. Uses the same transaction `c` as the
    update that triggered it, so the notification either lands WITH the
    shadow-hide or doesn't land at all (atomic)."""
    if not user_id:
        return
    try:
        c.execute(
            "INSERT INTO notifications "
            "(user_id, type, title, body, link_url, icon, metadata, created_at) "
            "VALUES (?, 'system', ?, ?, ?, 'alert-triangle', ?, ?)",
            (
                user_id,
                "Your take was hidden",
                (
                    "One of your takes got enough downvotes + a low enough "
                    "quality score that it's no longer shown to other users. "
                    "You can still see and edit it."
                ),
                f"/markets/{market_slug}#take-{take_id}",
                # Empty JSON object — metadata column is TEXT in the schema.
                "{}",
                int(time.time()),
            ),
        )
    except sqlite3.OperationalError:
        # notifications table not present (fresh DB, migration 026 not
        # applied on this branch). Treated as soft failure so the core
        # shadow-hide action still succeeds.
        return
    except Exception:
        # Never let a notification write break the vote pipeline.
        return


def cast_vote(take_id: int, user_id: int, vote: int) -> tuple[int, int]:
    """Record/replace a user's vote. Returns (upvotes, downvotes) AFTER update.

    `vote` must be +1 or -1. To clear a vote, call `clear_vote` instead.
    Authors voting on their own takes are silently ignored (returns current
    totals unchanged).

    Raises ValueError if vote value is invalid or take doesn't exist.
    """
    if vote not in (1, -1):
        raise ValueError("vote must be +1 or -1")

    take = get_take(take_id)
    if not take:
        raise ValueError("take not found")
    if take["user_id"] == user_id:
        # Self-vote: no-op. Not an error because the frontend shouldn't
        # show the buttons to authors in the first place, but belt +
        # braces.
        return int(take["upvotes"] or 0), int(take["downvotes"] or 0)

    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO take_votes (user_id, take_id, vote, voted_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, take_id) DO UPDATE SET vote = excluded.vote, "
            "  voted_at = excluded.voted_at",
            (user_id, take_id, vote, now),
        )
        _apply_vote_effects(c, take_id)
        row = c.execute(
            "SELECT upvotes, downvotes FROM market_takes WHERE id = ?",
            (take_id,),
        ).fetchone()
    return int(row["upvotes"] or 0), int(row["downvotes"] or 0)


def clear_vote(take_id: int, user_id: int) -> tuple[int, int]:
    """Remove a user's vote on a take, if any. Returns (upvotes, downvotes)."""
    with db.conn() as c:
        c.execute(
            "DELETE FROM take_votes WHERE take_id = ? AND user_id = ?",
            (take_id, user_id),
        )
        _apply_vote_effects(c, take_id)
        row = c.execute(
            "SELECT upvotes, downvotes FROM market_takes WHERE id = ?",
            (take_id,),
        ).fetchone()
    if row is None:
        return 0, 0
    return int(row["upvotes"] or 0), int(row["downvotes"] or 0)


def get_user_vote(take_id: int, user_id: int) -> Optional[int]:
    """Return +1, -1, or None."""
    with db.conn() as c:
        row = c.execute(
            "SELECT vote FROM take_votes WHERE take_id = ? AND user_id = ?",
            (take_id, user_id),
        ).fetchone()
    return int(row["vote"]) if row else None


def get_user_votes_for_market(market_slug: str, user_id: int) -> dict[int, int]:
    """Return {take_id: +1|-1} for every take the user has voted on in a
    given market. Used to paint the UI on page load so the user's current
    vote state survives a hard refresh.
    """
    slug = (market_slug or "").strip()
    if not slug or not user_id:
        return {}
    with db.conn() as c:
        rows = c.execute(
            "SELECT tv.take_id, tv.vote FROM take_votes tv "
            "JOIN market_takes mt ON mt.id = tv.take_id "
            "WHERE mt.market_slug = ? AND tv.user_id = ?",
            (slug, user_id),
        ).fetchall()
    return {int(r["take_id"]): int(r["vote"]) for r in rows}


# ── Reporting ───────────────────────────────────────────────────────────────


def create_report(
    *,
    take_id: int,
    reporter_user_id: int,
    reason: str,
    details: Optional[str] = None,
) -> Optional[int]:
    """Report a take. Returns the new report id, or None if the reporter
    already reported this take (dedup via UNIQUE index — not an error)."""
    reason = (reason or "").strip()[:64]
    if not reason:
        raise ValueError("reason required")
    details = (details or "").strip()[:1000] or None
    now = int(time.time())
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO take_reports "
                "(take_id, reporter_user_id, reason, details, reported_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (take_id, reporter_user_id, reason, details, now),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        # Duplicate report from same user — silent no-op (idempotent).
        return None


def list_open_reports(*, limit: int = 100) -> list[sqlite3.Row]:
    """Admin queue view: oldest unresolved reports first."""
    with db.conn() as c:
        return list(c.execute(
            "SELECT tr.*, mt.reasoning AS take_reasoning, mt.position AS take_position, "
            "       mt.market_slug AS take_market_slug, mt.is_deleted AS take_deleted, "
            "       mt.user_id AS take_user_id "
            "FROM take_reports tr "
            "LEFT JOIN market_takes mt ON mt.id = tr.take_id "
            "WHERE tr.resolved = 0 ORDER BY tr.reported_at ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall())


def resolve_report(
    report_id: int,
    *,
    admin_user_id: int,
    admin_action: str,
) -> bool:
    """Mark a single report resolved. `admin_action ∈ {deleted, dismissed,
    warned_user}`."""
    admin_action = (admin_action or "").strip()
    if admin_action not in ("deleted", "dismissed", "warned_user"):
        raise ValueError("admin_action must be deleted|dismissed|warned_user")
    with db.conn() as c:
        cur = c.execute(
            "UPDATE take_reports SET resolved = 1, admin_action = ?, "
            "  resolved_by = ?, resolved_at = ? WHERE id = ? AND resolved = 0",
            (admin_action, admin_user_id, int(time.time()), report_id),
        )
        return cur.rowcount > 0


def resolve_all_reports_for_take(
    take_id: int,
    *,
    admin_user_id: int,
    admin_action: str = "deleted",
) -> int:
    """When a take is hard-deleted, auto-resolve every open report on it.

    Returns the number of reports resolved."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE take_reports SET resolved = 1, admin_action = ?, "
            "  resolved_by = ?, resolved_at = ? WHERE take_id = ? AND resolved = 0",
            (admin_action, admin_user_id, int(time.time()), take_id),
        )
        return cur.rowcount


# ── Market resolution (invoked by the daily job) ───────────────────────────


def _outcome_to_position(outcome: Any) -> Optional[str]:
    """Coerce a market-outcome column into our take-position space."""
    if outcome is None:
        return None
    s = str(outcome).strip().lower()
    if s in ("yes", "y", "true", "1", "resolved_yes"):
        return "yes"
    if s in ("no", "n", "false", "0", "resolved_no"):
        return "no"
    return None


def list_unresolved_takes_for_market(market_slug: str) -> list[sqlite3.Row]:
    """Takes on a market that haven't been scored yet (resolved_correct IS NULL)."""
    slug = (market_slug or "").strip()
    if not slug:
        return []
    with db.conn() as c:
        return list(c.execute(
            "SELECT * FROM market_takes "
            "WHERE market_slug = ? AND is_deleted = 0 AND resolved_correct IS NULL",
            (slug,),
        ).fetchall())


def mark_takes_resolved_for_market(
    market_slug: str,
    outcome: Any,
) -> dict[str, int]:
    """Score every un-resolved take for a market based on its outcome.

    `outcome` is the raw column from the markets table; mapped via
    `_outcome_to_position`. Neutral markets (outcome unrecognised) are
    skipped — the take keeps its NULL resolved_correct and will be
    considered again on the next job run.

    Also refreshes quality_score with the correctness multiplier + toggles
    shadow_hidden if the new score drops below the threshold.

    Returns {"scored": n_written, "correct": n_correct, "incorrect": n_wrong}.
    """
    mapped = _outcome_to_position(outcome)
    if mapped is None:
        return {"scored": 0, "correct": 0, "incorrect": 0}

    takes = list_unresolved_takes_for_market(market_slug)
    if not takes:
        return {"scored": 0, "correct": 0, "incorrect": 0}

    correct = 0
    incorrect = 0
    # Batch in one transaction.
    with db.conn() as c:
        for t in takes:
            tpos = t["position"]
            if tpos == "neutral":
                # Neutral takes never "match" a directional outcome — score
                # them as 0 (not counted as wrong, not counted as right).
                c.execute(
                    "UPDATE market_takes SET resolved_correct = NULL WHERE id = ?",
                    (t["id"],),
                )
                continue
            is_correct = 1 if tpos == mapped else 0
            if is_correct:
                correct += 1
            else:
                incorrect += 1
            c.execute(
                "UPDATE market_takes SET resolved_correct = ? WHERE id = ?",
                (is_correct, t["id"]),
            )
            # Recompute with the correctness multiplier now baked in.
            _apply_vote_effects(c, t["id"])

    return {"scored": correct + incorrect, "correct": correct, "incorrect": incorrect}


# ── Stats for /settings/takes ──────────────────────────────────────────────


def user_take_stats(user_id: int) -> dict[str, Any]:
    """Aggregate stats shown on /settings/takes.

    total          — non-deleted takes the user has posted
    correct        — resolved_correct = 1
    incorrect      — resolved_correct = 0
    unresolved     — resolved_correct IS NULL (either market not done yet
                     or the user's position was 'neutral')
    correct_rate   — correct / (correct + incorrect), None if denominator 0
    avg_quality    — mean quality_score across all non-deleted takes, None
                     if no takes have been scored yet
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN resolved_correct = 1 THEN 1 ELSE 0 END) AS correct, "
            "  SUM(CASE WHEN resolved_correct = 0 THEN 1 ELSE 0 END) AS incorrect, "
            "  SUM(CASE WHEN resolved_correct IS NULL THEN 1 ELSE 0 END) AS unresolved, "
            "  AVG(quality_score) AS avg_quality "
            "FROM market_takes WHERE user_id = ? AND is_deleted = 0",
            (user_id,),
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    correct = int(row["correct"] or 0) if row else 0
    incorrect = int(row["incorrect"] or 0) if row else 0
    unresolved = int(row["unresolved"] or 0) if row else 0
    resolved_total = correct + incorrect
    rate = (correct / resolved_total) if resolved_total > 0 else None
    avg_q = (
        float(row["avg_quality"])
        if (row and row["avg_quality"] is not None) else None
    )
    return {
        "total": total,
        "correct": correct,
        "incorrect": incorrect,
        "unresolved": unresolved,
        "correct_rate": rate,
        "avg_quality": avg_q,
    }


# ── Resolution-run log ─────────────────────────────────────────────────────


def start_resolution_run() -> int:
    """Begin a new resolver run, return its id."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO take_resolution_runs (started_at, status) VALUES (?, 'running')",
            (int(time.time()),),
        )
        return int(cur.lastrowid)


def finish_resolution_run(
    run_id: int,
    *,
    markets_considered: int,
    takes_resolved: int,
    takes_correct: int,
    takes_incorrect: int,
    error: Optional[str] = None,
) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE take_resolution_runs SET "
            "  finished_at = ?, markets_considered = ?, takes_resolved = ?, "
            "  takes_correct = ?, takes_incorrect = ?, status = ?, error = ? "
            "WHERE id = ?",
            (
                int(time.time()),
                markets_considered,
                takes_resolved,
                takes_correct,
                takes_incorrect,
                "failed" if error else "ok",
                error,
                run_id,
            ),
        )
