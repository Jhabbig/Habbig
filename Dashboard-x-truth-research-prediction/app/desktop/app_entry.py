from __future__ import annotations
import os, secrets, sys, threading, time, webbrowser
from pathlib import Path

# Figure out where we're running from — inside .app bundle or dev
if getattr(sys, 'frozen', False):
    # Running inside PyInstaller bundle
    _bundle_dir = Path(sys._MEIPASS)
    _app_dir = _bundle_dir
    # Set working directory to a writable location next to the .app
    _data_dir = Path(os.path.expanduser("~/Library/Application Support/PolymarketDashboard"))
    _data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(str(_data_dir))
else:
    _app_dir = Path(__file__).resolve().parent.parent.parent
    _data_dir = _app_dir

if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18789
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
STARTUP_TIMEOUT = 20


def _ensure_env():
    """Create a default .env if one doesn't exist in the data directory."""
    env_path = _data_dir / ".env"
    if not env_path.exists():
        generated_password = secrets.token_urlsafe(16)
        env_path.write_text(
            "# Polymarket Signal Dashboard Configuration\n"
            "# Edit this file to add your API credentials\n\n"
            "TWITTER_BEARER_TOKEN=\n"
            "TWITTER_MONTHLY_QUOTA=10000\n"
            "TRUTHSOCIAL_USERNAME=\n"
            "TRUTHSOCIAL_PASSWORD=\n"
            "TRUTHSOCIAL_ACCESS_TOKEN=\n"
            "TRUTHSOCIAL_API_BASE_URL=https://truthsocial.com\n\n"
            "DASHBOARD_USER=admin\n"
            f"DASHBOARD_PASSWORD={generated_password}\n\n"
            "DATABASE_URL=sqlite+aiosqlite:///./predictions.db\n"
            "LOG_LEVEL=INFO\n"
        )
    # Also ensure config.yaml exists
    config_path = _data_dir / "app" / "config.yaml"
    if not config_path.exists():
        # Copy from bundle
        bundle_config = _app_dir / "app" / "config.yaml"
        if bundle_config.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(str(bundle_config), str(config_path))


def start_server():
    import uvicorn
    from app.main import app as fastapi_app
    uvicorn.run(fastapi_app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


def wait_for_server(timeout=STARTUP_TIMEOUT):
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{SERVER_URL}/health", timeout=1)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    _ensure_env()

    # Start FastAPI in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    if not wait_for_server():
        print("ERROR: Server failed to start.", file=sys.stderr)
        webbrowser.open(SERVER_URL)
        return

    # Open in default browser
    webbrowser.open(SERVER_URL)
    print(f"Polymarket Signal Dashboard running at {SERVER_URL}")
    print("Login credentials are in your .env file (change password in Profile after login)")
    print("Press Ctrl+C to quit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
