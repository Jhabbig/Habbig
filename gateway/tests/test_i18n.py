"""Tests for the i18n foundation — translator, detector, set-language route.

Covers:
  * t() fallback chain (requested → en → raw key)
  * _machine-flagged entries unwrap to their .text
  * normalise_lang handles pt_BR / pt_br / pt / PT-BR
  * detector precedence (query > user > cookie > Accept-Language > default)
  * /api/set-language cookie + authed-user persistence
  * malformed locale JSON doesn't crash anything
"""

from __future__ import annotations

USES_TESTDB = True

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _testdb  # noqa: F401 — shared in-memory DB

import db
from i18n import (
    DEFAULT,
    LANG_COOKIE_NAME,
    SUPPORTED,
    clear_cache,
    detect_language,
    load_locale,
    normalise_lang,
    parse_accept_language,
    t,
)


# ── Fake request ────────────────────────────────────────────────────────────


class _FakeQP:
    def __init__(self, d: dict | None = None):
        self._d = d or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakeState:
    def __init__(self, user=None):
        self.user = user


class _FakeRequest:
    def __init__(
        self,
        *,
        query: dict | None = None,
        cookies: dict | None = None,
        headers: dict | None = None,
        user: dict | None = None,
    ):
        self.query_params = _FakeQP(query or {})
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.state = _FakeState(user=user)


# ── Translator ──────────────────────────────────────────────────────────────


class TestTranslator(unittest.TestCase):

    def setUp(self):
        clear_cache()

    def test_english_direct_hit(self):
        self.assertEqual(t("btn.save", "en"), "Save")

    def test_spanish_unwraps_machine_flag(self):
        # es.json ships machine-flagged entries — translator must unwrap
        # {"text": "...", "_machine": true} and return the plain string.
        self.assertEqual(t("btn.save", "es"), "Guardar")

    def test_german_unwraps_machine_flag(self):
        self.assertEqual(t("btn.save", "de"), "Speichern")

    def test_ptbr_unwraps_machine_flag(self):
        self.assertEqual(t("btn.save", "pt-br"), "Salvar")

    def test_fallback_to_english(self):
        # Not a real key — every locale falls back to en, which also
        # doesn't have it, so the raw key comes out.
        self.assertEqual(t("definitely.not.a.key", "es"), "definitely.not.a.key")

    def test_fallback_missing_es_but_present_in_en(self):
        # Simulate an es-only key gap: clear the es cache, patch its
        # load_locale return to drop this one key, and check the
        # fallback path pulls from en.json.
        with patch("i18n.translator.load_locale") as mock_load:
            def fake(lang):
                if lang == "es":
                    return {}
                if lang == "en":
                    return {"nav.billing": "Billing"}
                return {}
            mock_load.side_effect = fake
            self.assertEqual(t("nav.billing", "es"), "Billing")

    def test_placeholder_substitution(self):
        # billing.access.trader has {remaining} / {total}
        out = t("billing.access.trader", "en", remaining=3, total=5)
        self.assertIn("3", out)
        self.assertIn("5", out)

    def test_placeholder_missing_kwarg_returns_template(self):
        # Missing kwarg — don't crash, return the unformatted string.
        out = t("billing.access.trader", "en", remaining=3)
        self.assertIn("{total}", out)

    def test_lang_none_falls_back_to_default(self):
        self.assertEqual(t("btn.save", None), "Save")  # type: ignore[arg-type]


class TestNormaliseLang(unittest.TestCase):

    def test_exact_match(self):
        self.assertEqual(normalise_lang("en"), "en")
        self.assertEqual(normalise_lang("es"), "es")
        self.assertEqual(normalise_lang("de"), "de")
        self.assertEqual(normalise_lang("pt-br"), "pt-br")

    def test_underscore_variant(self):
        self.assertEqual(normalise_lang("pt_BR"), "pt-br")
        self.assertEqual(normalise_lang("pt_br"), "pt-br")

    def test_case_insensitive(self):
        self.assertEqual(normalise_lang("PT-BR"), "pt-br")
        self.assertEqual(normalise_lang("EN"), "en")

    def test_primary_tag_fallback(self):
        # pt alone → pt-br because that's the only pt we support.
        self.assertEqual(normalise_lang("pt"), "pt-br")
        # en-US → en
        self.assertEqual(normalise_lang("en-US"), "en")

    def test_unsupported_returns_none(self):
        self.assertIsNone(normalise_lang("fr"))
        self.assertIsNone(normalise_lang("zh-cn"))
        self.assertIsNone(normalise_lang(""))
        self.assertIsNone(normalise_lang(None))


class TestAcceptLanguage(unittest.TestCase):

    def test_single(self):
        self.assertEqual(parse_accept_language("es"), [("es", 1.0)])

    def test_weighted(self):
        got = parse_accept_language("en-US,en;q=0.9,es;q=0.8")
        self.assertEqual(got, [("en-us", 1.0), ("en", 0.9), ("es", 0.8)])

    def test_malformed_q_defaults_to_zero(self):
        got = parse_accept_language("fr;q=bogus,en")
        self.assertEqual(got[0], ("en", 1.0))

    def test_empty_header(self):
        self.assertEqual(parse_accept_language(""), [])


# ── Detector ────────────────────────────────────────────────────────────────


class TestDetector(unittest.TestCase):

    def test_default_on_empty_request(self):
        self.assertEqual(detect_language(_FakeRequest()), DEFAULT)

    def test_query_param_wins(self):
        req = _FakeRequest(
            query={"lang": "es"},
            cookies={LANG_COOKIE_NAME: "de"},
            user={"preferred_language": "pt-br"},
            headers={"accept-language": "de,en;q=0.9"},
        )
        self.assertEqual(detect_language(req), "es")

    def test_user_pref_beats_cookie(self):
        req = _FakeRequest(
            cookies={LANG_COOKIE_NAME: "de"},
            user={"preferred_language": "pt-br"},
        )
        self.assertEqual(detect_language(req), "pt-br")

    def test_cookie_beats_header(self):
        req = _FakeRequest(
            cookies={LANG_COOKIE_NAME: "de"},
            headers={"accept-language": "es,en;q=0.9"},
        )
        self.assertEqual(detect_language(req), "de")

    def test_accept_language_parsed(self):
        req = _FakeRequest(headers={"accept-language": "fr,es;q=0.9,en;q=0.8"})
        # fr unsupported → es (highest supported q)
        self.assertEqual(detect_language(req), "es")

    def test_invalid_query_falls_through(self):
        req = _FakeRequest(
            query={"lang": "fr"},           # unsupported
            cookies={LANG_COOKIE_NAME: "es"},
        )
        self.assertEqual(detect_language(req), "es")

    def test_malformed_user_dict_does_not_crash(self):
        req = _FakeRequest(user={"preferred_language": ""})
        self.assertEqual(detect_language(req), DEFAULT)


# ── Malformed locale safety ────────────────────────────────────────────────


class TestMalformedLocale(unittest.TestCase):

    def test_missing_file_returns_empty_dict(self):
        clear_cache()
        # "xx" locale file definitely doesn't exist.
        self.assertEqual(load_locale("xx"), {})
        # And t() still returns the key or an English fallback.
        self.assertEqual(t("btn.save", "xx"), "Save")

    def test_broken_json_does_not_crash(self):
        clear_cache()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{ this is not json", encoding="utf-8")
            with patch("i18n.translator.LOCALES_DIR", Path(tmp)):
                self.assertEqual(load_locale("bad"), {})


# ── /api/set-language route ────────────────────────────────────────────────


class TestSetLanguageRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server
        import server_features  # noqa: F401
        from starlette.testclient import TestClient

        cls.server = server
        cls.client = TestClient(server.app)

        # Ensure preferred_language column exists even if migration 125
        # didn't run in this fresh test DB.
        try:
            with db.conn() as c:
                c.execute("ALTER TABLE users ADD COLUMN preferred_language TEXT DEFAULT 'en'")
        except Exception:
            pass  # column probably already exists

        # Test user with a session.
        with db.conn() as c:
            row = c.execute(
                "SELECT id FROM users WHERE email = 'i18n_tester@test.com'"
            ).fetchone()
        if row:
            cls.user_id = row["id"]
        else:
            cls.user_id = db.create_user(
                "i18n_tester@test.com", "TestPass1!", "i18n_tester"
            )

        token = db.create_session(cls.user_id)
        cls.cookies = {server.COOKIE_NAME: token, "_csrf": "csrf_t"}
        cls.csrf_headers = {"x-csrf-token": "csrf_t"}

    def test_rejects_unsupported_lang(self):
        r = self.client.post(
            "/api/set-language?lang=klingon",
            json={},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("supported", r.json())

    def test_query_param_sets_cookie(self):
        r = self.client.post(
            "/api/set-language?lang=es",
            json={},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["lang"], "es")
        self.assertTrue(body["ok"])
        # Cookie was set on the response.
        self.assertIn(LANG_COOKIE_NAME, r.cookies)
        self.assertEqual(r.cookies[LANG_COOKIE_NAME], "es")

    def test_json_body_sets_lang(self):
        r = self.client.post(
            "/api/set-language",
            json={"lang": "de"},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["lang"], "de")

    def test_authed_user_persists_to_db(self):
        r = self.client.post(
            "/api/set-language?lang=pt-br",
            json={},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["persisted"])
        with db.conn() as c:
            row = c.execute(
                "SELECT preferred_language FROM users WHERE id = ?",
                (self.user_id,),
            ).fetchone()
        self.assertEqual(row["preferred_language"], "pt-br")

    def test_underscore_locale_normalised(self):
        r = self.client.post(
            "/api/set-language?lang=pt_BR",
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["lang"], "pt-br")


if __name__ == "__main__":
    unittest.main()
