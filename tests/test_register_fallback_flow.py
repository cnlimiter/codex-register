from types import SimpleNamespace

import pytest

from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core import register
from src.services.base import BaseEmailService


class DummyEmailService(BaseEmailService):
    def __init__(self):
        super().__init__(EmailServiceType.TEMPMAIL, name="dummy")

    def create_email(self, config=None):
        return {"email": "tester@example.com", "service_id": "svc-1"}

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
        return "123456"

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True

    def refresh_session(self):
        return None


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class BrokenJSONResponse(FakeResponse):
    def json(self):
        raise ValueError("bad json")


class FakeSession:
    def __init__(self, post_handler=None, get_handler=None, cookies=None):
        self.post_handler = post_handler
        self.get_handler = get_handler
        self.cookies = cookies or {}
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "kwargs": kwargs})
        if self.post_handler is None:
            raise AssertionError("unexpected post call")
        return self.post_handler(url, **kwargs)

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, "kwargs": kwargs})
        if self.get_handler is None:
            raise AssertionError("unexpected get call")
        return self.get_handler(url, **kwargs)


class DummyHTTPClient:
    def __init__(self, proxy_url=None):
        self.proxy_url = proxy_url
        self.session = FakeSession()
        self.closed = False

    def close(self):
        self.closed = True

    def post(self, url, **kwargs):
        raise AssertionError("unexpected http client post")


class DummyOAuthManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start_oauth(self):
        return SimpleNamespace(
            auth_url="https://auth.example/start",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost/callback",
        )


def make_engine(monkeypatch, email_service=None):
    monkeypatch.setattr(
        register,
        "get_settings",
        lambda: SimpleNamespace(
            openai_client_id="client-id",
            openai_auth_url="https://auth.example/authorize",
            openai_token_url="https://auth.example/token",
            openai_redirect_uri="http://localhost/callback",
            openai_scope="openid email profile offline_access",
        ),
    )
    monkeypatch.setattr(register, "OpenAIHTTPClient", DummyHTTPClient)
    monkeypatch.setattr(register, "OAuthManager", DummyOAuthManager)

    engine = register.RegistrationEngine(email_service or DummyEmailService())
    engine.email = "tester@example.com"
    engine.email_info = {"email": "tester@example.com", "service_id": "svc-1"}
    return engine


@pytest.mark.parametrize(
    "page_type",
    [
        "login_password",
        OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        "consent_required",
        "some_other_page",
    ],
)
def test_submit_login_form_accepts_any_http_200_page(monkeypatch, page_type):
    engine = make_engine(monkeypatch)
    engine.session = FakeSession(
        post_handler=lambda url, **kwargs: FakeResponse(
            status_code=200,
            payload={"page": {"type": page_type}},
        )
    )

    result = engine._submit_login_form("did-1", "sen-1")

    assert result.success is True
    assert result.page_type == page_type
    assert result.error_message == ""


def test_submit_login_form_accepts_http_200_even_when_json_is_invalid(monkeypatch):
    engine = make_engine(monkeypatch)
    engine.session = FakeSession(
        post_handler=lambda url, **kwargs: BrokenJSONResponse(status_code=200)
    )

    result = engine._submit_login_form("did-1", "sen-1")

    assert result.success is True
    assert result.page_type == ""
    assert result.response_data == {}
    assert result.error_message == ""


def test_send_passwordless_otp_posts_empty_body(monkeypatch):
    engine = make_engine(monkeypatch)
    engine.session = FakeSession(
        post_handler=lambda url, **kwargs: FakeResponse(status_code=200)
    )

    success = engine._send_passwordless_otp()

    assert success is True
    assert len(engine.session.post_calls) == 1
    call = engine.session.post_calls[0]
    assert call["url"] == OPENAI_API_ENDPOINTS["send_passwordless_otp"]
    assert call["kwargs"]["data"] == ""
    assert engine._otp_sent_at is not None


def test_send_passwordless_otp_does_not_update_timestamp_on_failure(monkeypatch):
    engine = make_engine(monkeypatch)
    engine._otp_sent_at = 1234.5
    engine.session = FakeSession(
        post_handler=lambda url, **kwargs: FakeResponse(status_code=500, text="server error")
    )

    success = engine._send_passwordless_otp()

    assert success is False
    assert engine._otp_sent_at == 1234.5


def test_get_verification_code_passes_explicit_otp_timestamp(monkeypatch):
    captured = {}

    class RecordingEmailService(DummyEmailService):
        def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
            captured["email"] = email
            captured["email_id"] = email_id
            captured["timeout"] = timeout
            captured["pattern"] = pattern
            captured["otp_sent_at"] = otp_sent_at
            return "654321"

    engine = make_engine(monkeypatch, email_service=RecordingEmailService())

    code = engine._get_verification_code(otp_sent_at=1234.5)

    assert code == "654321"
    assert captured["email"] == "tester@example.com"
    assert captured["email_id"] == "svc-1"
    assert captured["timeout"] == 120
    assert captured["otp_sent_at"] == 1234.5


def test_validate_verification_code_accepts_http_200_even_when_json_is_invalid(monkeypatch):
    engine = make_engine(monkeypatch)
    engine.session = FakeSession(
        post_handler=lambda url, **kwargs: BrokenJSONResponse(status_code=200)
    )

    result = engine._validate_verification_code("123456")

    assert result.success is True
    assert result.continue_url == ""
    assert result.response_data == {}


def test_run_closes_http_client_on_early_failure(monkeypatch):
    engine = make_engine(monkeypatch)
    tracking_client = DummyHTTPClient()
    engine.http_client = tracking_client

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (False, None))

    result = engine.run()

    assert result.success is False
    assert tracking_client.closed is True
    assert engine.session is None


def test_fallback_to_login_flow_forces_otp_and_continue_url(monkeypatch):
    engine = make_engine(monkeypatch)
    steps = []
    captured = {}

    monkeypatch.setattr(engine, "_reset_oauth_session", lambda: steps.append("reset_session") or True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: steps.append("get_device_id") or "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda did: steps.append("check_sentinel") or "sen-1")
    monkeypatch.setattr(
        engine,
        "_submit_login_form",
        lambda did, sen: steps.append("submit_login_form")
        or register.SignupFormResult(success=True, page_type="login_password"),
    )

    def fake_send_passwordless_otp():
        steps.append("send_passwordless_otp")
        engine._otp_sent_at = 4567.89
        return True

    def fake_get_verification_code(otp_sent_at=None):
        steps.append("get_verification_code")
        captured["otp_sent_at"] = otp_sent_at
        return "123456"

    monkeypatch.setattr(engine, "_send_passwordless_otp", fake_send_passwordless_otp)
    monkeypatch.setattr(engine, "_get_verification_code", fake_get_verification_code)
    monkeypatch.setattr(
        engine,
        "_validate_verification_code",
        lambda code: steps.append("validate_verification_code")
        or register.OTPValidationResult(success=True, continue_url="https://auth.example/continue"),
    )

    def fake_try_upgrade(continue_url, stage):
        steps.append("get_continue_url_and_parse_workspace")
        captured["continue_url"] = continue_url
        captured["stage"] = stage
        return "ws-123"

    monkeypatch.setattr(engine, "_try_upgrade_cookie_with_continue_url", fake_try_upgrade)

    workspace_id = engine._fallback_to_login_flow()

    assert workspace_id == "ws-123"
    assert steps == [
        "reset_session",
        "get_device_id",
        "check_sentinel",
        "submit_login_form",
        "send_passwordless_otp",
        "get_verification_code",
        "validate_verification_code",
        "get_continue_url_and_parse_workspace",
    ]
    assert captured["continue_url"] == "https://auth.example/continue"
    assert captured["stage"] == "降级登录 Continue URL"
    assert captured["otp_sent_at"] == 4567.89


def test_fallback_to_login_flow_requires_continue_url(monkeypatch):
    engine = make_engine(monkeypatch)

    monkeypatch.setattr(engine, "_reset_oauth_session", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda did: "sen-1")
    monkeypatch.setattr(
        engine,
        "_submit_login_form",
        lambda did, sen: register.SignupFormResult(success=True, page_type="login_password"),
    )

    def fake_send_passwordless_otp():
        engine._otp_sent_at = 9876.5
        return True

    monkeypatch.setattr(engine, "_send_passwordless_otp", fake_send_passwordless_otp)
    monkeypatch.setattr(engine, "_get_verification_code", lambda otp_sent_at=None: "123456")
    monkeypatch.setattr(
        engine,
        "_validate_verification_code",
        lambda code: register.OTPValidationResult(success=True, continue_url=""),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("continue_url 缺失时不应尝试升级 Cookie")

    monkeypatch.setattr(engine, "_try_upgrade_cookie_with_continue_url", fail_if_called)

    assert engine._fallback_to_login_flow() is None


def test_fallback_to_login_flow_accepts_workspace_without_continue_url(monkeypatch):
    engine = make_engine(monkeypatch)
    steps = []

    monkeypatch.setattr(engine, "_reset_oauth_session", lambda: steps.append("reset_session") or True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: steps.append("get_device_id") or "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda did: steps.append("check_sentinel") or "sen-1")
    monkeypatch.setattr(
        engine,
        "_submit_login_form",
        lambda did, sen: steps.append("submit_login_form")
        or register.SignupFormResult(success=True, page_type="login_password"),
    )

    def fake_send_passwordless_otp():
        steps.append("send_passwordless_otp")
        engine._otp_sent_at = 4567.89
        return True

    monkeypatch.setattr(engine, "_send_passwordless_otp", fake_send_passwordless_otp)
    monkeypatch.setattr(
        engine,
        "_get_verification_code",
        lambda otp_sent_at=None: steps.append("get_verification_code") or "123456",
    )
    monkeypatch.setattr(
        engine,
        "_validate_verification_code",
        lambda code: steps.append("validate_verification_code")
        or register.OTPValidationResult(success=True, continue_url=""),
    )
    monkeypatch.setattr(
        engine,
        "_get_workspace_id",
        lambda log_missing=True: steps.append("get_workspace_id") or "ws-cookie",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("已有 workspace 时不应继续访问 continue_url")

    monkeypatch.setattr(engine, "_try_upgrade_cookie_with_continue_url", fail_if_called)

    workspace_id = engine._fallback_to_login_flow()

    assert workspace_id == "ws-cookie"
    assert steps == [
        "reset_session",
        "get_device_id",
        "check_sentinel",
        "submit_login_form",
        "send_passwordless_otp",
        "get_verification_code",
        "validate_verification_code",
        "get_workspace_id",
    ]


def test_get_verification_code_uses_provider_timeout_and_refreshes_once(monkeypatch):
    captured = {"calls": [], "refresh_count": 0}

    class RefreshableOutlookService(DummyEmailService):
        def __init__(self):
            super().__init__()
            self.service_type = EmailServiceType.OUTLOOK

        def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
            captured["calls"].append(
                {
                    "timeout": timeout,
                    "otp_sent_at": otp_sent_at,
                    "email": email,
                    "email_id": email_id,
                }
            )
            if len(captured["calls"]) == 1:
                return None
            return "987654"

        def refresh_session(self):
            captured["refresh_count"] += 1

    engine = make_engine(monkeypatch, email_service=RefreshableOutlookService())

    code = engine._get_verification_code(otp_sent_at=2468.0)

    assert code == "987654"
    assert captured["refresh_count"] == 1
    assert len(captured["calls"]) == 2
    assert captured["calls"][0]["timeout"] == 180
    assert captured["calls"][1]["timeout"] == 180
    assert captured["calls"][0]["otp_sent_at"] == 2468.0


def test_try_upgrade_cookie_with_continue_url_retries_with_second_probe(monkeypatch):
    engine = make_engine(monkeypatch)
    sleep_calls = []
    workspace_results = iter([None, None, None, None, None, "ws-delayed"])
    engine.session = FakeSession(
        get_handler=lambda url, **kwargs: FakeResponse(status_code=302),
        cookies={},
    )

    monkeypatch.setattr(engine, "_log_cookie_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_get_workspace_id", lambda log_missing=False: next(workspace_results))
    monkeypatch.setattr(register.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    workspace_id = engine._try_upgrade_cookie_with_continue_url(
        "https://auth.example/continue",
        "降级登录 Continue URL",
    )

    assert workspace_id == "ws-delayed"
    assert len(engine.session.get_calls) == 3
    assert sleep_calls == [1.0, 2.0, 4.0]
