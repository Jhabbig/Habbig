from __future__ import annotations
import webbrowser, pathlib
import rumps

class PolymarketMenuBar(rumps.App):
    def __init__(self, server_url: str):
        icon_path = pathlib.Path(__file__).parent / "assets" / "menubar_icon.png"
        super().__init__(name="Polymarket Dashboard", icon=str(icon_path) if icon_path.exists() else None, template=True, quit_button=None)
        self.server_url = server_url
        self.menu = [rumps.MenuItem("Open Dashboard", callback=self._open_dashboard), rumps.MenuItem("Refresh Now", callback=self._refresh_feed), None, rumps.MenuItem("Status: Running", callback=None), None, rumps.MenuItem("Quit", callback=self._quit_app)]

    def _open_dashboard(self, _):
        webbrowser.open(self.server_url)

    def _refresh_feed(self, _):
        import requests
        try:
            resp = requests.get(f"{self.server_url}/refresh", timeout=30)
            if resp.status_code == 200:
                rumps.notification(title="Pipeline Complete", subtitle="Feed refreshed", message="Check the dashboard for new predictions.")
            else:
                rumps.notification(title="Refresh Warning", subtitle=f"Status {resp.status_code}", message="Pipeline may have issues.")
        except Exception as exc:
            rumps.notification("Refresh Failed", "", str(exc))

    def _quit_app(self, _):
        rumps.quit_application()
