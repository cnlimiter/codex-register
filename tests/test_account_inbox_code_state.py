import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.core.register import RegistrationEngine, RegistrationResult
from src.database.models import Account, Base
from src.database.session import DatabaseSessionManager
from src.services.base import BaseEmailService
from src.web.routes import accounts as accounts_routes


class DummySettings:
    openai_client_id = "client-1"
    openai_auth_url = "https://auth.openai.test/authorize"
    openai_token_url = "https://auth.openai.test/token"
    openai_redirect_uri = "https://localhost/callback"
    openai_scope = "openid profile email offline_access"
    tempmail_base_url = "https://api.tempmail.test"
    tempmail_timeout = 30
    tempmail_max_retries = 3


class FakeStatefulTempmailService(BaseEmailService):
    def __init__(self, config=None, name=None):
        super().__init__(EmailServiceType.TEMPMAIL, name)
        self.messages = [
            ("id:msg-1", "111111"),
            ("id:msg-2", "222222"),
        ]

    def create_email(self, config=None):
        return {"email": "tester@example.com", "service_id": "token-1"}

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at=None,
    ):
        for marker, code in self.messages:
            if self._accept_verification_code(email, code, marker):
                return code
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def _build_test_db(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_account_inbox_code_persists_verification_state_across_requests(monkeypatch):
    manager = _build_test_db("account_inbox_code_state.db")

    with manager.session_scope() as session:
        account = Account(
            email="tester@example.com",
            email_service="tempmail",
            email_service_id="token-1",
            status="active",
            extra_data={},
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = account.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(accounts_routes, "get_db", fake_get_db)
    monkeypatch.setattr(accounts_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(
        "src.services.base.EmailServiceFactory.create",
        lambda service_type, config, name=None: FakeStatefulTempmailService(config, name),
    )

    first = asyncio.run(accounts_routes.get_account_inbox_code(account_id))
    second = asyncio.run(accounts_routes.get_account_inbox_code(account_id))

    assert first["success"] is True
    assert first["code"] == "111111"
    assert second["success"] is True
    assert second["code"] == "222222"

    with manager.session_scope() as session:
        saved = session.query(Account).filter(Account.id == account_id).first()
        verification_state = (saved.extra_data or {}).get("verification_state") or {}
        assert verification_state["used_codes"] == ["111111", "222222"]
        assert verification_state["seen_messages"] == ["id:msg-1", "id:msg-2"]


def test_save_to_database_persists_verification_state(monkeypatch):
    manager = _build_test_db("registration_verification_state.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)
    monkeypatch.setattr("src.core.register.get_settings", lambda: DummySettings())

    email_service = FakeStatefulTempmailService()
    email_service._accept_verification_code("tester@example.com", "111111", "id:msg-1")

    engine = RegistrationEngine(email_service=email_service, proxy_url="http://proxy.test")
    engine.email_info = {"service_id": "token-1"}

    result = RegistrationResult(
        success=True,
        email="tester@example.com",
        password="secret",
        account_id="acct-1",
        workspace_id="ws-1",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
        session_token="session-token",
        metadata={"registered_at": "2026-03-26T00:00:00"},
        source="register",
    )

    assert engine.save_to_database(result) is True

    with manager.session_scope() as session:
        saved = session.query(Account).filter(Account.email == "tester@example.com").first()
        verification_state = (saved.extra_data or {}).get("verification_state") or {}
        assert verification_state["used_codes"] == ["111111"]
        assert verification_state["seen_messages"] == ["id:msg-1"]
