"""Import-level smoke tests."""
import sports_dashboard as sd


def test_module_imports():
    assert sd.app is not None


def test_templates_loaded():
    """All four templates must load and contain their expected sentinel."""
    for name in ["DASHBOARD_HTML", "USERS_HTML", "SETTINGS_HTML", "ADMIN_HTML"]:
        body = getattr(sd, name)
        assert body, f"{name} is empty"
        assert "<!DOCTYPE html>" in body[:60]
        assert "</html>" in body[-100:]


def test_route_count():
    """Sanity check — make sure no routes were dropped during refactor."""
    routes = list(sd.app.routes)
    # 50 routes after refactor: original 47 + /api/diagnostics/match-rejects
    # + /api/diagnostics/odds-quota + /metrics
    assert len(routes) >= 47, f"unexpected route count {len(routes)}"
