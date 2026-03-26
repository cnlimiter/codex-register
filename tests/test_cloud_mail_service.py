from datetime import datetime, timezone

from src.services.cloud_mail import CloudMailService


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


def _to_timestamp(value: str) -> float:
    normalized = value.replace(" ", "T")
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def test_cloud_mail_creates_address_via_public_api():
    service = CloudMailService({
        "base_url": "https://mail.example.com",
        "admin_email": "admin@example.com",
        "admin_password": "admin-secret",
        "default_domain": "mail.example.com",
    })
    service.http_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": {"token": "public-token"},
            }
        ),
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": None,
            }
        ),
    ])

    result = service.create_email()

    assert result["email"].endswith("@mail.example.com")
    assert result["service_id"] == result["email"]
    assert result["id"] == result["email"]
    assert result["password"]

    first_call = service.http_client.calls[0]
    second_call = service.http_client.calls[1]

    assert first_call["method"] == "POST"
    assert first_call["url"] == "https://mail.example.com/api/public/genToken"
    assert first_call["kwargs"]["json"] == {
        "email": "admin@example.com",
        "password": "admin-secret",
    }

    assert second_call["method"] == "POST"
    assert second_call["url"] == "https://mail.example.com/api/public/addUser"
    assert second_call["kwargs"]["headers"]["Authorization"] == "public-token"
    assert second_call["kwargs"]["json"]["list"][0]["email"] == result["email"]


def test_cloud_mail_extracts_openai_verification_code_from_public_email_list():
    service = CloudMailService({
        "base_url": "https://mail.example.com",
        "admin_email": "admin@example.com",
        "admin_password": "admin-secret",
        "default_domain": "mail.example.com",
    })
    service.http_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": {"token": "public-token"},
            }
        ),
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 1,
                        "sendEmail": "noreply@openai.com",
                        "sendName": "OpenAI",
                        "subject": "Your OpenAI verification code",
                        "text": "Your OpenAI verification code is 654321",
                        "content": "",
                    }
                ],
            }
        ),
    ])

    code = service.get_verification_code(
        email="tester@mail.example.com",
        timeout=1,
    )

    assert code == "654321"


def test_cloud_mail_ignores_messages_received_before_otp_sent_at():
    service = CloudMailService({
        "base_url": "https://mail.example.com",
        "admin_email": "admin@example.com",
        "admin_password": "admin-secret",
        "default_domain": "mail.example.com",
    })
    service.http_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": {"token": "public-token"},
            }
        ),
        FakeResponse(
            payload={
                "code": 200,
                "message": "success",
                "data": [
                    {
                        "emailId": 1,
                        "sendEmail": "noreply@openai.com",
                        "sendName": "OpenAI",
                        "subject": "Old code",
                        "text": "111111",
                        "content": "",
                        "createTime": "2026-03-23 10:00:00",
                    },
                    {
                        "emailId": 2,
                        "sendEmail": "noreply@openai.com",
                        "sendName": "OpenAI",
                        "subject": "New code",
                        "text": "222222",
                        "content": "",
                        "createTime": "2026-03-23 10:00:05",
                    },
                ],
            }
        ),
    ])

    code = service.get_verification_code(
        email="tester@mail.example.com",
        timeout=1,
        otp_sent_at=_to_timestamp("2026-03-23T10:00:02Z"),
    )

    assert code == "222222"
