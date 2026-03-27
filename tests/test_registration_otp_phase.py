import src.core.register as register_module
from src.core.register import (
    ERROR_OTP_TIMEOUT_SECONDARY,
    PhaseContext,
    PhaseResult,
    RegistrationEngine,
)
from src.services import EmailServiceType


class DummySettings:
    openai_client_id = "client-id"
    openai_auth_url = "https://auth.example.test"
    openai_token_url = "https://token.example.test"
    openai_redirect_uri = "https://callback.example.test"
    openai_scope = "openid profile email"
    email_code_timeout = 120
    email_code_poll_interval = 3
    email_code_resend_max_retries = 2
    email_code_non_openai_sender_resend_max_retries = 1


class FakeEmailService:
    def __init__(self, code):
        self.service_type = EmailServiceType.TEMPMAIL
        self.code = code
        self.calls = []

    def get_verification_code(self, **kwargs):
        self.calls.append(kwargs)
        return self.code


class FastResendEmailService(FakeEmailService):
    def get_verification_code(self, **kwargs):
        self.calls.append(kwargs)
        raise register_module.OTPNoOpenAISenderEmailServiceError()


class FakeCookies:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


class FakeSession:
    def __init__(self, cookies=None):
        self.cookies = FakeCookies(cookies or {})
        self.get_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("unexpected network call")


class FakeResponse:
    def __init__(self, *, url="", text="", json_payload=None):
        self.url = url
        self.text = text
        self._json_payload = json_payload

    def json(self):
        if isinstance(self._json_payload, Exception):
            raise self._json_payload
        return self._json_payload


def _build_engine(monkeypatch, email_service, **setting_overrides):
    def _settings():
        settings = DummySettings()
        for key, value in setting_overrides.items():
            setattr(settings, key, value)
        return settings

    monkeypatch.setattr(register_module, "get_settings", _settings)
    return RegistrationEngine(email_service=email_service)


def _build_failed_phase(error_code: str, error_message: str) -> PhaseResult:
    return PhaseResult(
        phase=register_module.PHASE_OTP_SECONDARY,
        success=False,
        error_code=error_code,
        error_message=error_message,
        retryable=True,
        next_action="resend_otp",
    )


def _prepare_engine_for_run(monkeypatch, phase_results, **setting_overrides):
    engine = _build_engine(
        monkeypatch,
        FakeEmailService(code=None),
        **setting_overrides,
    )
    monkeypatch.setattr(register_module.time, "time", lambda: 100.0)

    send_calls = []
    phase_iter = iter(phase_results)

    def fake_phase_email_prepare():
        engine.email = "tester@example.com"
        engine.email_info = {"service_id": "svc-1"}
        return True

    monkeypatch.setattr(engine, "_phase_email_prepare", fake_phase_email_prepare)
    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "US"))
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: "did-1")
    monkeypatch.setattr(engine, "_check_sentinel", lambda _did: None)
    monkeypatch.setattr(
        engine,
        "_submit_signup_form",
        lambda _did, _sen_token: type("SignupResult", (), {"success": True, "error_message": ""})(),
    )
    monkeypatch.setattr(engine, "_register_password", lambda: (True, "pass-123"))

    def fake_send_verification_code():
        send_calls.append("send")
        engine._otp_sent_at = 100.0
        return True

    monkeypatch.setattr(engine, "_send_verification_code", fake_send_verification_code)
    monkeypatch.setattr(engine, "_phase_otp_secondary", lambda *_args, **_kwargs: (None, next(phase_iter)))
    return engine, send_calls


def test_phase_otp_secondary_uses_remaining_budget_from_start_timestamp(monkeypatch):
    email_service = FakeEmailService(code="654321")
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._phase_otp_secondary(
        PhaseContext(otp_sent_at=77.0),
        started_at=100.0,
    )

    assert code == "654321"
    assert phase_result.success is True
    assert email_service.calls[0]["timeout"] == 100
    assert email_service.calls[0]["otp_sent_at"] == 77.0
    assert email_service.calls[0]["email"] == "tester@example.com"
    assert email_service.calls[0]["email_id"] == "svc-1"


def test_phase_otp_secondary_returns_dedicated_timeout_error_code(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._phase_otp_secondary(
        PhaseContext(otp_sent_at=80.0),
        started_at=100.0,
    )

    assert code is None
    assert phase_result.success is False
    assert phase_result.error_code == ERROR_OTP_TIMEOUT_SECONDARY
    assert engine.phase_history[0].error_code == ERROR_OTP_TIMEOUT_SECONDARY


def test_phase_otp_secondary_maps_no_openai_sender_to_resend_action(monkeypatch):
    email_service = FastResendEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.email = "tester@example.com"
    engine.email_info = {"service_id": "svc-1"}

    monkeypatch.setattr(register_module.time, "time", lambda: 120.0)

    code, phase_result = engine._phase_otp_secondary(
        PhaseContext(otp_sent_at=80.0),
        started_at=100.0,
    )

    assert code is None
    assert phase_result.success is False
    assert phase_result.error_code == "OTP_NO_OPENAI_SENDER"
    assert phase_result.retryable is True
    assert phase_result.next_action == "resend_otp"


def test_run_uses_dedicated_budget_for_non_openai_sender_resends(monkeypatch):
    engine, send_calls = _prepare_engine_for_run(
        monkeypatch,
        [
            _build_failed_phase("OTP_NO_OPENAI_SENDER", "detected non-openai sender"),
            _build_failed_phase(ERROR_OTP_TIMEOUT_SECONDARY, "timeout after dedicated resend"),
        ],
        email_code_resend_max_retries=0,
        email_code_non_openai_sender_resend_max_retries=1,
    )

    result = engine.run()

    assert result.success is False
    assert result.error_code == ERROR_OTP_TIMEOUT_SECONDARY
    assert len(send_calls) == 2


def test_run_stops_when_non_openai_sender_budget_is_exhausted(monkeypatch):
    engine, send_calls = _prepare_engine_for_run(
        monkeypatch,
        [
            _build_failed_phase("OTP_NO_OPENAI_SENDER", "detected non-openai sender"),
            _build_failed_phase("OTP_NO_OPENAI_SENDER", "detected non-openai sender again"),
        ],
        email_code_resend_max_retries=2,
        email_code_non_openai_sender_resend_max_retries=1,
    )

    result = engine.run()

    assert result.success is False
    assert result.error_code == "OTP_NO_OPENAI_SENDER"
    assert len(send_calls) == 2


def test_run_keeps_timeout_budget_after_non_openai_sender_resend(monkeypatch):
    engine, send_calls = _prepare_engine_for_run(
        monkeypatch,
        [
            _build_failed_phase(ERROR_OTP_TIMEOUT_SECONDARY, "timeout #1"),
            _build_failed_phase("OTP_NO_OPENAI_SENDER", "detected non-openai sender"),
            _build_failed_phase(ERROR_OTP_TIMEOUT_SECONDARY, "timeout #2"),
            _build_failed_phase(ERROR_OTP_TIMEOUT_SECONDARY, "timeout #3"),
        ],
        email_code_resend_max_retries=2,
        email_code_non_openai_sender_resend_max_retries=1,
    )

    result = engine.run()

    assert result.success is False
    assert result.error_code == ERROR_OTP_TIMEOUT_SECONDARY
    assert len(send_calls) == 4


def test_advance_login_authorization_sets_otp_anchor_before_password_submit(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.oauth_start = object()
    engine._otp_sent_at = 10.0

    monkeypatch.setattr(register_module.time, "time", lambda: 456.0)
    monkeypatch.setattr(engine, "_init_session", lambda: True)
    monkeypatch.setattr(engine, "_start_oauth", lambda: True)
    monkeypatch.setattr(engine, "_get_device_id", lambda: True)
    monkeypatch.setattr(engine, "_try_reenter_login_flow", lambda: True)

    seen_anchors = []

    def fake_submit_login_password_step():
        seen_anchors.append(engine._otp_sent_at)
        return True

    monkeypatch.setattr(engine, "_submit_login_password_step", fake_submit_login_password_step)

    def fake_get_verification_code():
        seen_anchors.append(engine._otp_sent_at)
        return None

    monkeypatch.setattr(engine, "_get_verification_code", fake_get_verification_code)

    workspace_id, callback_url = engine._advance_login_authorization()

    assert workspace_id is None
    assert callback_url is None
    assert engine._otp_sent_at == 456.0
    assert seen_anchors == [456.0, 456.0]


def test_get_device_id_reuses_existing_cookie_without_extra_request(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    engine.oauth_start = type("OAuthStart", (), {"auth_url": "https://auth.example.test/authorize"})()
    engine.session = FakeSession(cookies={"oai-did": "did-cached"})

    assert engine._get_device_id() == "did-cached"
    assert engine.session.get_calls == []


def test_extract_workspace_id_from_response_payload(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    response = FakeResponse(
        url="https://auth.example.test/consent?workspace_id=ws-url",
        json_payload={
            "page": {
                "workspace": {
                    "id": "ws-json",
                }
            }
        },
    )

    assert engine._extract_workspace_id_from_response(response=response) == "ws-json"


def test_extract_workspace_id_from_response_text_when_hidden_input_missing(monkeypatch):
    email_service = FakeEmailService(code=None)
    engine = _build_engine(monkeypatch, email_service)
    response = FakeResponse(
        url="https://auth.example.test/consent",
        text='<script>window.__NEXT_DATA__={"activeWorkspaceId":"ws-script"}</script>',
        json_payload=ValueError("not json"),
    )

    assert engine._extract_workspace_id_from_response(response=response) == "ws-script"
