"""Regression tests for the IP_HASH_SALT hardening.

Adversarial audit finding (HIGH): the previous default value of
``_IP_HASH_SALT`` was the literal ``"narve.ai/analytics/v1"`` baked into
version control. Any attacker who exfiltrated the analytics DB could
precompute SHA-256 over the entire IPv4 space in a few CPU-hours and
reverse every ``ip_hash`` row to the originating IP.

This module locks in the fix:

* The salt is now read exclusively from the ``IP_HASH_SALT`` env var
  (``_IP_HASH_SALT_ENV``). In production startup raises if it is empty
  or shorter than 32 chars.
* In dev / tests an explicit ``_IP_HASH_SALT_DEV_FALLBACK`` is used and
  startup logs a WARNING — the fallback is intentionally not a
  "looks-cryptographic" constant.
* The old literal ``"narve.ai/analytics/v1"`` must not appear in
  ``server.py`` anywhere.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import unittest

# Match the env contract used by the rest of the test suite: drop the
# site gate and PRODUCTION flag so importing ``server`` does not blow up
# on the new IP_HASH_SALT startup guard (which only fires in production).
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestIpHashSaltSource(unittest.TestCase):
    """The salt MUST be sourced from the environment, with no production
    fallback to the old hardcoded constant. Tests here read ``server.py``
    text directly so we catch any future regression that re-introduces a
    literal default for ``IP_HASH_SALT``.
    """

    @classmethod
    def setUpClass(cls):
        cls.server_path = os.path.join(
            os.path.dirname(__file__), "..", "server.py"
        )
        with open(cls.server_path, "r", encoding="utf-8") as fh:
            cls.server_src = fh.read()

    def test_old_hardcoded_salt_literal_is_gone(self):
        # The exact literal that was published in
        # ENV_DEFAULTS_AUDIT.md as the rainbow-table risk surface.
        self.assertNotIn(
            "narve.ai/analytics/v1",
            self.server_src,
            "Old hardcoded IP_HASH_SALT literal must not be reintroduced. "
            "Use os.environ.get('IP_HASH_SALT', '') instead.",
        )

    def test_no_real_default_for_ip_hash_salt_env_lookup(self):
        # Defence-in-depth: catch any future
        # ``os.environ.get("IP_HASH_SALT", "<x>")`` call where <x> is
        # anything other than the empty string. The canonical form is
        # ``os.environ.get("IP_HASH_SALT", "")`` — empty fallback is what
        # the startup check relies on to refuse to boot in production.
        pattern = re.compile(
            r"""os\.environ\.get\(\s*["']IP_HASH_SALT["']\s*,\s*["']([^"']*)["']\s*\)"""
        )
        defaults = pattern.findall(self.server_src)
        self.assertGreaterEqual(
            len(defaults),
            1,
            "Expected at least one os.environ.get('IP_HASH_SALT', ...) call in server.py",
        )
        for default in defaults:
            self.assertEqual(
                default,
                "",
                "IP_HASH_SALT must have NO non-empty default value in source code "
                "(found default: %r)." % default,
            )

    def test_salt_env_var_overrides_dev_fallback(self):
        # When IP_HASH_SALT is set, _hash_ip() must use it (not the dev
        # fallback). We reload the server module under the env override
        # so we can observe the bound _IP_HASH_SALT.
        os.environ["IP_HASH_SALT"] = "a" * 64
        try:
            if "server" in sys.modules:
                server = importlib.reload(sys.modules["server"])
            else:
                import server  # noqa: F401
                server = sys.modules["server"]
            self.assertEqual(server._IP_HASH_SALT_ENV, "a" * 64)
            self.assertEqual(server._IP_HASH_SALT, "a" * 64)
            # Two different IPs must produce two different hashes
            # (sanity check that the salt is actually folded in).
            h1 = server._hash_ip("1.2.3.4")
            h2 = server._hash_ip("5.6.7.8")
            self.assertNotEqual(h1, h2)
        finally:
            os.environ.pop("IP_HASH_SALT", None)

    def test_salt_changes_with_env(self):
        # Same IP, different salts → different hashes. This is the
        # property that protects exfiltrated DBs against rainbow tables.
        os.environ["IP_HASH_SALT"] = "salt-one-" + ("x" * 32)
        if "server" in sys.modules:
            server = importlib.reload(sys.modules["server"])
        else:
            import server  # noqa: F401
            server = sys.modules["server"]
        h_with_salt_one = server._hash_ip("203.0.113.7")

        os.environ["IP_HASH_SALT"] = "salt-two-" + ("y" * 32)
        server = importlib.reload(sys.modules["server"])
        h_with_salt_two = server._hash_ip("203.0.113.7")

        os.environ.pop("IP_HASH_SALT", None)
        self.assertNotEqual(
            h_with_salt_one,
            h_with_salt_two,
            "Same IP with different salts must produce different hashes — "
            "otherwise the salt is not being applied.",
        )

    def test_dev_fallback_is_clearly_marked_not_secret(self):
        # If somebody removes the env-only contract and brings back a
        # source-level fallback, at least make sure the fallback string
        # screams "dev only" so it can't be mistaken for a real salt.
        if "server" in sys.modules:
            server = importlib.reload(sys.modules["server"])
        else:
            import server  # noqa: F401
            server = sys.modules["server"]
        self.assertIn("dev", server._IP_HASH_SALT_DEV_FALLBACK.lower())
        self.assertIn("not-secret", server._IP_HASH_SALT_DEV_FALLBACK.lower())


if __name__ == "__main__":
    unittest.main()
