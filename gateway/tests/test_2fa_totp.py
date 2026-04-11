"""Tests for security/two_factor.py helpers.

These are pure unit tests — no HTTP layer, no DB writes. The Fernet wrapper
reads CREDENTIALS_ENCRYPTION_KEY from env; we set a fresh key for the test.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set a fresh Fernet key before importing anything that touches encryption.
# Using a deterministic key across test runs so we can assert round-trips.
from cryptography.fernet import Fernet  # noqa: E402

os.environ["CREDENTIALS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from security import two_factor as tf  # noqa: E402
import pyotp  # noqa: E402


class TestTOTPRoundtrip(unittest.TestCase):
    def test_generate_secret_is_base32(self):
        secret = tf.generate_totp_secret()
        self.assertEqual(len(secret), 32)
        # base32 alphabet
        self.assertTrue(all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret))

    def test_encrypt_decrypt_roundtrip(self):
        secret = tf.generate_totp_secret()
        encrypted = tf.encrypt_totp_secret(secret)
        self.assertNotEqual(encrypted, secret)
        self.assertEqual(tf.decrypt_totp_secret(encrypted), secret)

    def test_build_uri_contains_issuer(self):
        secret = tf.generate_totp_secret()
        uri = tf.build_totp_uri(secret, "sho@narve.ai")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("narve.ai", uri)
        self.assertIn("sho%40narve.ai", uri)

    def test_build_qr_returns_data_uri(self):
        secret = tf.generate_totp_secret()
        uri = tf.build_totp_uri(secret, "sho@narve.ai")
        data_uri = tf.build_qr_data_uri(uri)
        self.assertTrue(data_uri.startswith("data:image/png;base64,"))

    def test_verify_known_vector(self):
        secret = tf.generate_totp_secret()
        # Generate the code via pyotp directly and ensure our verify accepts it
        code = pyotp.TOTP(secret).now()
        self.assertTrue(tf.verify_totp_code(secret, code))

    def test_verify_wrong_code_rejected(self):
        secret = tf.generate_totp_secret()
        self.assertFalse(tf.verify_totp_code(secret, "000000"))

    def test_verify_malformed_rejected(self):
        secret = tf.generate_totp_secret()
        self.assertFalse(tf.verify_totp_code(secret, ""))
        self.assertFalse(tf.verify_totp_code(secret, "abcdef"))
        self.assertFalse(tf.verify_totp_code(secret, "1234"))


class TestBackupCodeFormat(unittest.TestCase):
    def test_generate_returns_eight_unique(self):
        codes = tf.generate_backup_codes()
        self.assertEqual(len(codes), 8)
        self.assertEqual(len(set(codes)), 8)

    def test_format_is_xxxx_xxxx(self):
        import re
        codes = tf.generate_backup_codes()
        pattern = re.compile(r"^[A-F0-9]{4}-[A-F0-9]{4}$")
        for c in codes:
            self.assertRegex(c, pattern)

    def test_hash_contains_required_fields(self):
        code = tf.generate_backup_codes()[0]
        h = tf.hash_backup_code(code)
        self.assertIn("hash", h)
        self.assertIn("salt", h)
        self.assertIn("used_at", h)
        self.assertIsNone(h["used_at"])


class TestEmailOTP(unittest.TestCase):
    def test_generate_six_digits(self):
        code = tf.generate_email_otp()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_hash_verify_roundtrip(self):
        code = tf.generate_email_otp()
        h, salt = tf.hash_email_otp(code)
        self.assertTrue(tf.verify_email_otp_code(code, h, salt))
        self.assertFalse(tf.verify_email_otp_code("000000", h, salt))


if __name__ == "__main__":
    unittest.main()
