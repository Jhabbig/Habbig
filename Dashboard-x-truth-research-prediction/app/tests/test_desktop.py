from unittest.mock import MagicMock, patch
import time

def test_server_wait_success():
    n = 0
    def mock_get(url, timeout=1):
        nonlocal n; n += 1
        if n < 3: raise ConnectionError
        r = MagicMock(); r.status_code = 200; return r
    with patch("requests.get", side_effect=mock_get):
        from app.desktop.app_entry import wait_for_server
        assert wait_for_server(timeout=10) is True

def test_server_wait_timeout():
    with patch("requests.get", side_effect=ConnectionError):
        from app.desktop.app_entry import wait_for_server
        assert wait_for_server(timeout=2) is False

def test_refresh_calls_endpoint():
    mock_resp = MagicMock(); mock_resp.status_code = 200
    with patch("requests.get", return_value=mock_resp) as mg, patch("rumps.notification"):
        from app.desktop.menu_bar import PolymarketMenuBar
        with patch.object(PolymarketMenuBar, "__init__", lambda self, **kw: None):
            bar = PolymarketMenuBar.__new__(PolymarketMenuBar)
            bar.server_url = "http://127.0.0.1:18789"
            bar._refresh_feed(None)
        mg.assert_called_once()

def test_webview_creates_window():
    with patch("webview.create_window", return_value=MagicMock()) as mc, patch("webview.start"):
        from app.desktop.webview_window import open_webview
        open_webview("http://127.0.0.1:18789")
        assert mc.call_args.kwargs["url"] == "http://127.0.0.1:18789"
