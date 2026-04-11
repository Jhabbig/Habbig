"""Tests for the scraper FastAPI application."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient


# Patch DB init before importing the app
with patch("scraper.storage.db.init_db"), \
     patch("scraper.scheduler.start_scheduler"):
    from scraper.main import app

API_KEY = "test-api-key-for-testing-only-48chars-minimum-ok"
AUTH_HEADER = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def client():
    with patch("scraper.main.SCRAPER_API_KEY", API_KEY):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestAuth:
    def test_health_no_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 401

    def test_health_wrong_key(self, client):
        resp = client.get("/health", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_health_missing_bearer(self, client):
        resp = client.get("/health", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401

    @patch("scraper.main.twitter_scraper")
    @patch("scraper.main.truthsocial_scraper")
    @patch("scraper.main.store")
    @patch("scraper.main.get_scheduler_status")
    def test_health_valid_key(self, mock_sched, mock_store, mock_ts, mock_tw, client):
        mock_tw.health_check = AsyncMock(return_value={
            "platform": "twitter", "available": True, "session_valid": True,
            "last_successful_run": None, "posts_collected_today": 0, "error": None,
        })
        mock_ts.health_check = AsyncMock(return_value={
            "platform": "truthsocial", "available": True, "session_valid": False,
            "last_successful_run": None, "posts_collected_today": 0, "error": None,
        })
        mock_store.get_untransmitted_count.return_value = 5
        mock_sched.return_value = {"running": True, "jobs": []}

        resp = client.get("/health", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert data["untransmitted_count"] == 5
        assert data["twitter"]["available"] is True


class TestScheduler:
    @patch("scraper.main.get_scheduler_status")
    def test_scheduler_status(self, mock_sched, client):
        mock_sched.return_value = {
            "running": True,
            "jobs": [
                {"id": "twitter_scrape", "name": "Twitter Scrape",
                 "next_run": "2024-01-01T00:20:00+00:00", "interval_minutes": 20, "paused": False}
            ],
        }
        resp = client.get("/scheduler/status", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert len(data["jobs"]) == 1

    @patch("scraper.main.pause_job")
    def test_pause_job(self, mock_pause, client):
        mock_pause.return_value = True
        resp = client.post("/scheduler/pause/twitter_scrape", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["paused"] is True

    @patch("scraper.main.update_job_interval")
    def test_update_interval(self, mock_update, client):
        mock_update.return_value = True
        resp = client.patch(
            "/scheduler/interval/twitter_scrape",
            headers=AUTH_HEADER,
            json={"interval_minutes": 30},
        )
        assert resp.status_code == 200
        assert resp.json()["new_interval_minutes"] == 30

    @patch("scraper.main.update_job_interval")
    def test_update_interval_invalid(self, mock_update, client):
        resp = client.patch(
            "/scheduler/interval/twitter_scrape",
            headers=AUTH_HEADER,
            json={"interval_minutes": 0},
        )
        assert resp.status_code == 400


class TestKeywords:
    @patch("scraper.main.store")
    def test_list_keywords(self, mock_store, client):
        mock_store.get_keywords.return_value = {
            "twitter": ["will win", "predict"],
            "truthsocial": ["election"],
        }
        resp = client.get("/keywords", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert "twitter" in data
        assert "predict" in data["twitter"]

    @patch("scraper.main.store")
    def test_add_keyword(self, mock_store, client):
        mock_store.add_keyword.return_value = True
        resp = client.post("/keywords", headers=AUTH_HEADER, json={
            "platform": "twitter", "keyword": "new keyword"
        })
        assert resp.status_code == 200
        assert resp.json()["added"] is True

    @patch("scraper.main.store")
    def test_add_keyword_invalid_platform(self, mock_store, client):
        resp = client.post("/keywords", headers=AUTH_HEADER, json={
            "platform": "instagram", "keyword": "test"
        })
        assert resp.status_code == 400

    @patch("scraper.main.store")
    def test_delete_keyword(self, mock_store, client):
        mock_store.remove_keyword.return_value = True
        resp = client.delete("/keywords/twitter/predict", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @patch("scraper.main.store")
    def test_delete_keyword_not_found(self, mock_store, client):
        mock_store.remove_keyword.return_value = False
        resp = client.delete("/keywords/twitter/nonexistent", headers=AUTH_HEADER)
        assert resp.status_code == 404


class TestRuns:
    @patch("scraper.main.store")
    def test_list_runs(self, mock_store, client):
        mock_store.list_runs.return_value = []
        resp = client.get("/runs", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("scraper.main.store")
    def test_get_run_not_found(self, mock_store, client):
        mock_store.get_run.return_value = None
        resp = client.get("/runs/999", headers=AUTH_HEADER)
        assert resp.status_code == 404


class TestSessions:
    @patch("scraper.main.store")
    def test_list_sessions(self, mock_store, client):
        mock_store.list_sessions.return_value = []
        resp = client.get("/sessions", headers=AUTH_HEADER)
        assert resp.status_code == 200

    @patch("scraper.main.twitter_scraper")
    @patch("scraper.main.store")
    def test_validate_twitter_session(self, mock_store, mock_tw, client):
        mock_tw.is_available.return_value = False
        mock_store.get_session.return_value = None
        resp = client.post("/sessions/validate/twitter", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_validate_invalid_platform(self, client):
        resp = client.post("/sessions/validate/instagram", headers=AUTH_HEADER)
        assert resp.status_code == 400


class TestAdminIntegration:
    """Tests for the main server's /api/scraper/* proxy endpoints."""

    @patch("scraper.main.store")
    def test_posts_untransmitted(self, mock_store, client):
        mock_store.get_untransmitted.return_value = []
        resp = client.get("/posts/untransmitted", headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("scraper.main.store")
    def test_posts_acknowledge(self, mock_store, client):
        mock_store.mark_transmitted.return_value = 2
        resp = client.post("/posts/acknowledge", headers=AUTH_HEADER, json={
            "post_ids": ["twitter:1", "twitter:2"]
        })
        assert resp.status_code == 200
        assert resp.json()["acknowledged"] == 2
