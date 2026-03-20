from src.config.constants import EmailServiceType
from src.core.email_access import build_email_access_snapshot, inject_email_access_config
from src.database.models import Account
from src.services.duck_mail import DuckMailService
from src.web.routes.accounts import account_to_response


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"未准备响应: {method} {url}")
        return self.responses.pop(0)


def test_build_email_access_snapshot_for_duck_mail_includes_password_and_token():
    snapshot = build_email_access_snapshot(
        EmailServiceType.DUCK_MAIL,
        email="tester@duckmail.sbs",
        email_info={
            "service_id": "account-1",
            "account_id": "account-1",
            "password": "mail-pass",
            "token": "token-123",
        },
        service_config={
            "base_url": "https://api.duckmail.test",
            "default_domain": "duckmail.sbs",
        },
    )

    assert snapshot["email"] == "tester@duckmail.sbs"
    assert snapshot["service_id"] == "account-1"
    assert snapshot["account_id"] == "account-1"
    assert snapshot["password"] == "mail-pass"
    assert snapshot["token"] == "token-123"


def test_account_to_response_exposes_email_password_from_extra_data():
    account = Account(
        id=1,
        email="tester@duckmail.sbs",
        password="openai-pass",
        email_service="duck_mail",
        status="active",
        extra_data={
            "email_access": {
                "email": "tester@duckmail.sbs",
                "password": "mail-pass",
            }
        },
    )

    response = account_to_response(account)

    assert response.email_login == "tester@duckmail.sbs"
    assert response.email_password == "mail-pass"


def test_duck_mail_service_reads_verification_code_from_persisted_access():
    config = inject_email_access_config(
        EmailServiceType.DUCK_MAIL,
        {
            "base_url": "https://api.duckmail.test",
            "default_domain": "duckmail.sbs",
        },
        {
            "email": "tester@duckmail.sbs",
            "service_id": "account-1",
            "account_id": "account-1",
            "password": "mail-pass",
            "token": "token-123",
        },
    )

    service = DuckMailService(config)
    service.http_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "hydra:member": [
                    {
                        "id": "msg-1",
                        "from": {
                            "name": "OpenAI",
                            "address": "noreply@openai.com",
                        },
                        "subject": "Your verification code",
                        "createdAt": "2026-03-19T10:00:00Z",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "id": "msg-1",
                "text": "Your OpenAI verification code is 654321",
                "html": [],
            }
        ),
    ])

    code = service.get_verification_code(
        email="tester@duckmail.sbs",
        email_id="account-1",
        timeout=1,
    )

    assert code == "654321"
