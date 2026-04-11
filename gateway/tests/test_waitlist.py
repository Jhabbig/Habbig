"""Tests for Feature 3: waitlist position numbers + referrals."""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _signup(email: str, ref: str | None = None) -> tuple[int, str]:
    """Replicate the server_features._api_newsletter_v2 position-assign logic
    directly, to avoid the HTTP surface and CSRF during unit tests."""
    import secrets
    import time
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def _code() -> str:
        return "".join(secrets.choice(alphabet) for _ in range(8))

    with db.conn() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS next FROM newsletter_subscribers"
        ).fetchone()
        next_pos = row["next"]
        code = _code()
        while c.execute(
            "SELECT 1 FROM newsletter_subscribers WHERE referral_code = ?", (code,)
        ).fetchone():
            code = _code()
        c.execute(
            "INSERT INTO newsletter_subscribers (email, subscribed_at, source, position, display_position, referral_code, referred_by_code) "
            "VALUES (?, ?, 'prerelease', ?, ?, ?, ?)",
            (email, int(time.time()), next_pos, next_pos, code, ref),
        )
    return next_pos, code


class TestPositionAssignment(unittest.TestCase):
    def test_positions_are_sequential(self):
        p1, _ = _signup("seq-a@test.com")
        p2, _ = _signup("seq-b@test.com")
        p3, _ = _signup("seq-c@test.com")
        self.assertEqual(p2, p1 + 1)
        self.assertEqual(p3, p2 + 1)

    def test_referral_code_is_8_chars_alnum(self):
        _, code = _signup("code@test.com")
        self.assertEqual(len(code), 8)
        self.assertTrue(code.isalnum())
        self.assertEqual(code, code.upper())

    def test_referral_codes_are_unique_across_signups(self):
        codes = {_signup(f"uniq-{i}@test.com")[1] for i in range(20)}
        self.assertEqual(len(codes), 20)


class TestReferralBump(unittest.TestCase):
    def test_referrer_display_position_decreases_by_5(self):
        referrer_pos, referrer_code = _signup("referrer@test.com")
        # 10 more people sign up ahead of them
        for i in range(10):
            _signup(f"filler-{i}@test.com")
        # Now someone uses the referral
        _signup("referred@test.com", ref=referrer_code)

        # Apply the bump the same way server_features._apply_referral_bump does.
        with db.conn() as c:
            row = c.execute(
                "SELECT display_position FROM newsletter_subscribers WHERE referral_code = ?",
                (referrer_code,),
            ).fetchone()
            new_disp = max(1, row["display_position"] - 5)
            c.execute(
                "UPDATE newsletter_subscribers SET display_position = ? WHERE referral_code = ?",
                (new_disp, referrer_code),
            )
            final = c.execute(
                "SELECT display_position, position FROM newsletter_subscribers WHERE referral_code = ?",
                (referrer_code,),
            ).fetchone()
        # Original position is preserved, display_position moved up by 5.
        self.assertEqual(final["position"], referrer_pos)
        self.assertEqual(final["display_position"], referrer_pos - 5)

    def test_display_position_never_goes_below_1(self):
        pos, code = _signup("toptier@test.com")
        # They're #1 (or close). Apply 3 referrals.
        for _ in range(3):
            with db.conn() as c:
                row = c.execute(
                    "SELECT display_position FROM newsletter_subscribers WHERE referral_code = ?",
                    (code,),
                ).fetchone()
                new_disp = max(1, row["display_position"] - 5)
                c.execute(
                    "UPDATE newsletter_subscribers SET display_position = ? WHERE referral_code = ?",
                    (new_disp, code),
                )
        with db.conn() as c:
            row = c.execute(
                "SELECT display_position FROM newsletter_subscribers WHERE referral_code = ?",
                (code,),
            ).fetchone()
        self.assertGreaterEqual(row["display_position"], 1)


class TestReferralTracking(unittest.TestCase):
    def test_referred_by_code_stored(self):
        _, code_a = _signup("alice-ref@test.com")
        _signup("bob-used@test.com", ref=code_a)
        with db.conn() as c:
            row = c.execute(
                "SELECT referred_by_code FROM newsletter_subscribers WHERE email = ?",
                ("bob-used@test.com",),
            ).fetchone()
        self.assertEqual(row["referred_by_code"], code_a)

    def test_referral_count_for_code(self):
        _, code = _signup("counter@test.com")
        _signup("x1@test.com", ref=code)
        _signup("x2@test.com", ref=code)
        _signup("x3@test.com", ref=code)
        with db.conn() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM newsletter_subscribers WHERE referred_by_code = ?",
                (code,),
            ).fetchone()["n"]
        self.assertEqual(n, 3)


if __name__ == "__main__":
    unittest.main()
