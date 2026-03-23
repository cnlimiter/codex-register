import base64
import json
from types import SimpleNamespace

from src.core.login import LoginEngine


def _build_auth_cookie(workspace_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.signature"


def test_get_workspace_id_retries_with_exponential_backoff(monkeypatch):
    engine = LoginEngine.__new__(LoginEngine)
    engine.logs = []
    engine._log = lambda message, level="info": engine.logs.append((level, message))

    auth_cookie = _build_auth_cookie("ws-123")
    cookies = SimpleNamespace()
    calls = {"count": 0}

    def fake_get(name):
        assert name == "oai-client-auth-session"
        calls["count"] += 1
        if calls["count"] < 4:
            return None
        return auth_cookie

    cookies.get = fake_get
    engine.session = SimpleNamespace(cookies=cookies)

    sleeps = []
    monkeypatch.setattr("src.core.login.time.sleep", lambda seconds: sleeps.append(seconds))

    workspace_id = engine._get_workspace_id()

    assert workspace_id == "ws-123"
    assert calls["count"] == 4
    assert sleeps == [1, 2, 4]


def test_run_always_closes_resources_on_early_return():
    engine = LoginEngine.__new__(LoginEngine)
    engine.logs = []
    engine._log = lambda message, level="info": None
    engine.close_called = False
    engine.close = lambda: setattr(engine, "close_called", True)

    engine._check_ip_location = lambda: (False, "blocked")

    result = engine.run()

    assert result.success is False
    assert result.error_message == "IP 地理位置不支持: blocked"
    assert engine.close_called is True
