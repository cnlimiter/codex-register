from src.services import EmailServiceType
from src.services.base import BaseEmailService


class DummyEmailService(BaseEmailService):
    def __init__(self):
        super().__init__(EmailServiceType.TEMPMAIL, name="dummy")

    def create_email(self, config=None):
        return {"email": "dummy@example.com", "service_id": "dummy"}

    def get_verification_code(self, **kwargs):
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def test_is_openai_candidate_message_supports_sender_and_content_paths():
    service = DummyEmailService()

    assert service._is_openai_candidate_message("noreply@openai.com", "hello") is True
    assert service._is_openai_candidate_message("notice@example.com", "Your OpenAI verification code") is True
    assert service._is_openai_candidate_message("notice@example.com", "newsletter") is False


def test_batch_has_openai_sender_only_checks_sender_fields():
    service = DummyEmailService()
    batch = [
        {"from": "notice@example.com", "body": "openai mentioned in content"},
        {"from": "alerts@example.com", "body": "still not sender"},
    ]

    assert service._batch_has_openai_sender(batch, lambda item: item.get("from")) is False
    assert service._batch_has_openai_sender(
        batch + [{"from": "otp@tm1.openai.com", "body": "code"}],
        lambda item: item.get("from"),
    ) is True

