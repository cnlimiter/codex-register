from src.services import EmailServiceType
from src.services.outlook.base import EmailMessage, ProviderType
from src.services.outlook.email_parser import EmailParser
from src.services.outlook.health_checker import HealthChecker
from src.services.outlook.service import OutlookService
from src.services.base import OTPNoOpenAISenderEmailServiceError
from src.web.routes import registration as registration_routes


def test_health_checker_is_scoped_by_account_email():
    checker = HealthChecker(failure_threshold=1, disable_duration=120)

    checker.record_failure(ProviderType.IMAP_OLD, "boom", account_email="a@example.com")

    assert checker.is_available(ProviderType.IMAP_OLD, "b@example.com") is True
    assert checker.is_available(ProviderType.IMAP_OLD, "a@example.com") is False

    account_a_status = checker.get_all_health_status("a@example.com")
    assert account_a_status[ProviderType.IMAP_OLD.value]["status"] == "disabled"


def test_email_parser_respects_recipient_match_switch():
    parser = EmailParser()
    mail = EmailMessage(
        id="m1",
        subject="Your verification code",
        sender="noreply@openai.com",
        recipients=["other@example.com"],
        body="Your code is 123456",
        received_timestamp=123,
    )

    assert parser.is_openai_verification_email(
        mail,
        target_email="target@example.com",
        require_recipient_match=True,
    ) is False
    assert parser.is_openai_verification_email(
        mail,
        target_email="target@example.com",
        require_recipient_match=False,
    ) is True

    assert parser.find_verification_code_in_emails(
        [mail],
        target_email="target@example.com",
        require_recipient_match=True,
    ) is None
    assert parser.find_verification_code_in_emails(
        [mail],
        target_email="target@example.com",
        require_recipient_match=False,
    ) == "123456"


def test_normalize_outlook_config_inherits_global_settings(monkeypatch):
    class DummySettings:
        outlook_provider_priority = ["imap_old", "graph_api"]
        outlook_health_failure_threshold = 4
        outlook_health_disable_duration = 90
        outlook_require_recipient_match = True

    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    normalized = registration_routes._normalize_email_service_config(
        EmailServiceType.OUTLOOK,
        {
            "email": "user@example.com",
            "require_recipient_match": False,
        },
        proxy_url="http://127.0.0.1:7890",
    )

    assert normalized["provider_priority"] == ["imap_old", "graph_api"]
    assert normalized["health_failure_threshold"] == 4
    assert normalized["health_disable_duration"] == 90
    assert normalized["require_recipient_match"] is False
    assert normalized["proxy_url"] == "http://127.0.0.1:7890"


def test_email_parser_has_openai_sender():
    parser = EmailParser()
    mails = [
        EmailMessage(id="x1", subject="hello", sender="notice@example.com"),
        EmailMessage(id="x2", subject="otp", sender="no-reply@tm.openai.com"),
    ]
    assert parser.has_openai_sender(mails) is True


def test_outlook_service_returns_early_when_batch_has_no_openai_sender(monkeypatch):
    service = OutlookService(
        {
            "email": "user@example.com",
            "password": "pwd",
        },
        name="outlook-test",
    )

    non_openai_emails = [
        EmailMessage(
            id="m1",
            subject="newsletter",
            sender="newsletter@example.com",
            recipients=["user@example.com"],
            body="no code",
            received_timestamp=100,
        )
    ]

    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda *args, **kwargs: non_openai_emails,
    )

    try:
        service.get_verification_code(
            email="user@example.com",
            timeout=30,
            otp_sent_at=0,
        )
    except OTPNoOpenAISenderEmailServiceError:
        return

    raise AssertionError("expected OTPNoOpenAISenderEmailServiceError")


