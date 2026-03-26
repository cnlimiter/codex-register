from src.services.duck_mail import DuckMailService
from src.services.freemail import FreemailService
from src.services.moe_mail import MeoMailEmailService
from src.services.temp_mail import TempMailService
from src.services.tempmail import TempmailService


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


class FakeRequestHTTPClient:
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


class FakeGetHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({
            "method": "GET",
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"未准备响应: GET {url}")
        return self.responses.pop(0)


def test_tempmail_service_skips_code_returned_by_previous_fetch():
    service = TempmailService({"base_url": "https://api.tempmail.test"})
    service.http_client = FakeGetHTTPClient([
        FakeResponse(
            payload={
                "emails": [
                    {
                        "date": 1000,
                        "from": "noreply@openai.com",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "emails": [
                    {
                        "date": 1000,
                        "from": "noreply@openai.com",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                    },
                    {
                        "date": 1003,
                        "from": "noreply@openai.com",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 654321",
                    },
                ]
            }
        ),
    ])

    first_code = service.get_verification_code(
        email="tester@example.com",
        email_id="token-1",
        timeout=1,
        otp_sent_at=1000,
    )
    second_code = service.get_verification_code(
        email="tester@example.com",
        email_id="token-1",
        timeout=1,
        otp_sent_at=1002,
    )

    assert first_code == "111111"
    assert second_code == "654321"


def test_temp_mail_service_skips_code_returned_by_previous_fetch():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    service.http_client = FakeRequestHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "msg-1",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-19T10:00:00Z",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "msg-1",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-19T10:00:00Z",
                    },
                    {
                        "id": "msg-2",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 654321",
                        "createdAt": "2026-03-19T10:00:03Z",
                    },
                ]
            }
        ),
    ])

    first_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378400,
    )
    second_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378402,
    )

    assert first_code == "111111"
    assert second_code == "654321"


def test_temp_mail_service_accepts_same_code_from_newer_message():
    service = TempMailService({
        "base_url": "https://mail.example.com",
        "admin_password": "admin-secret",
        "domain": "example.com",
    })
    service.http_client = FakeRequestHTTPClient([
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "msg-1",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-19T10:00:00Z",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "msg-1",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-19T10:00:00Z",
                    },
                    {
                        "id": "msg-2",
                        "source": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "body": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-19T10:00:03Z",
                    },
                ]
            }
        ),
    ])

    first_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378400,
    )
    second_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378402,
    )

    assert first_code == "111111"
    assert second_code == "111111"


def test_freemail_service_skips_code_returned_by_previous_fetch():
    service = FreemailService({
        "base_url": "https://mail.example.com",
        "admin_token": "jwt-token",
    })
    service.http_client = FakeRequestHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "msg-1",
                    "sender": "noreply@openai.com",
                    "subject": "Your verification code",
                    "preview": "Your OpenAI verification code is 111111",
                    "verification_code": "111111",
                    "created_at": "2026-03-19T10:00:00Z",
                }
            ]
        ),
        FakeResponse(
            payload=[
                {
                    "id": "msg-1",
                    "sender": "noreply@openai.com",
                    "subject": "Your verification code",
                    "preview": "Your OpenAI verification code is 111111",
                    "verification_code": "111111",
                    "created_at": "2026-03-19T10:00:00Z",
                },
                {
                    "id": "msg-2",
                    "sender": "noreply@openai.com",
                    "subject": "Your verification code",
                    "preview": "Your OpenAI verification code is 654321",
                    "verification_code": "654321",
                    "created_at": "2026-03-19T10:00:03Z",
                },
            ]
        ),
    ])

    first_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378400,
    )
    second_code = service.get_verification_code(
        email="tester@example.com",
        timeout=1,
        otp_sent_at=1742378402,
    )

    assert first_code == "111111"
    assert second_code == "654321"


def test_duck_mail_service_skips_previously_used_code_even_with_small_timestamp_gap():
    service = DuckMailService({
        "base_url": "https://api.duckmail.test",
        "default_domain": "duckmail.sbs",
    })
    service.http_client = FakeRequestHTTPClient([
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
                        "createdAt": "2026-03-19T10:00:01Z",
                    }
                ]
            }
        ),
        FakeResponse(
            payload={
                "id": "msg-1",
                "text": "Your OpenAI verification code is 111111",
                "html": [],
            }
        ),
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
                        "createdAt": "2026-03-19T10:00:01Z",
                    },
                    {
                        "id": "msg-2",
                        "from": {
                            "name": "OpenAI",
                            "address": "noreply@openai.com",
                        },
                        "subject": "Your verification code",
                        "createdAt": "2026-03-19T10:00:03Z",
                    },
                ]
            }
        ),
        FakeResponse(
            payload={
                "id": "msg-2",
                "text": "Your OpenAI verification code is 654321",
                "html": [],
            }
        ),
        FakeResponse(
            payload={
                "id": "msg-1",
                "text": "Your OpenAI verification code is 111111",
                "html": [],
            }
        ),
    ])
    service._accounts_by_email["tester@duckmail.sbs"] = {
        "email": "tester@duckmail.sbs",
        "service_id": "account-1",
        "account_id": "account-1",
        "token": "token-123",
    }

    first_code = service.get_verification_code(
        email="tester@duckmail.sbs",
        email_id="account-1",
        timeout=1,
        otp_sent_at=1742378401,
    )
    second_code = service.get_verification_code(
        email="tester@duckmail.sbs",
        email_id="account-1",
        timeout=1,
        otp_sent_at=1742378402,
    )

    assert first_code == "111111"
    assert second_code == "654321"


def test_moe_mail_service_filters_old_messages_with_millisecond_timestamps():
    service = MeoMailEmailService({
        "base_url": "https://mail.example.com",
        "api_key": "api-key",
    })

    def fake_make_request(method, endpoint, **kwargs):
        if endpoint == "/api/emails/email-1":
            return {
                "messages": [
                    {
                        "id": "msg-old",
                        "from_address": "noreply@openai.com",
                        "subject": "Your verification code",
                        "received_at": 1742378400000,
                    },
                    {
                        "id": "msg-new",
                        "from_address": "noreply@openai.com",
                        "subject": "Your verification code",
                        "received_at": 1742378403000,
                    },
                ]
            }
        if endpoint == "/api/emails/email-1/msg-old":
            return {
                "message": {
                    "content": "Your OpenAI verification code is 111111",
                }
            }
        if endpoint == "/api/emails/email-1/msg-new":
            return {
                "message": {
                    "content": "Your OpenAI verification code is 654321",
                }
            }
        raise AssertionError(f"未准备响应: {method} {endpoint}")

    service._make_request = fake_make_request

    code = service.get_verification_code(
        email="tester@example.com",
        email_id="email-1",
        timeout=1,
        otp_sent_at=1742378402,
    )

    assert code == "654321"
