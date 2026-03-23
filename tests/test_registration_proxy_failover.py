from types import SimpleNamespace

from src.database import crud
from src.database.session import DatabaseSessionManager
from src.web.routes import registration
from src.core.register import RegistrationResult


def test_run_sync_registration_task_disables_bad_proxy_and_retries(monkeypatch, tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/test.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        primary_proxy = crud.create_proxy(
            session,
            name="primary",
            type="http",
            host="127.0.0.1",
            port=8001,
        )
        crud.update_proxy(session, primary_proxy.id, is_default=True)
        backup_proxy = crud.create_proxy(
            session,
            name="backup",
            type="http",
            host="127.0.0.1",
            port=8002,
        )
        email_service = crud.create_email_service(
            session,
            service_type="tempmail",
            name="tempmail-db",
            config={"base_url": "https://mail.example/api"},
        )
        crud.create_registration_task(session, task_uuid="task-proxy-failover")
        primary_proxy_id = primary_proxy.id
        backup_proxy_id = backup_proxy.id
        email_service_id = email_service.id

    monkeypatch.setattr(registration, "get_db", manager.session_scope)
    monkeypatch.setattr(
        registration,
        "EmailServiceFactory",
        SimpleNamespace(create=lambda service_type, config: SimpleNamespace(service_type=service_type, config=config)),
    )

    attempted_proxies = []
    saved_results = []

    class FakeRegistrationEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.proxy_url = proxy_url

        def run(self):
            attempted_proxies.append(self.proxy_url)
            if self.proxy_url.endswith(":8001"):
                return RegistrationResult(
                    success=False,
                    email="proxy@example.com",
                    error_message="OpenAI 请求失败: curl: (35) TLS handshake failed",
                )

            return RegistrationResult(
                success=True,
                email="proxy@example.com",
                access_token="access-token",
                workspace_id="ws-123",
            )

        def save_to_database(self, result):
            saved_results.append(result.email)
            return True

    monkeypatch.setattr(registration, "RegistrationEngine", FakeRegistrationEngine)

    registration._run_sync_registration_task(
        task_uuid="task-proxy-failover",
        email_service_type="tempmail",
        proxy=None,
        email_service_config=None,
        email_service_id=email_service_id,
    )

    assert attempted_proxies == [
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002",
    ]
    assert saved_results == ["proxy@example.com"]

    with manager.session_scope() as session:
        disabled_primary = crud.get_proxy_by_id(session, primary_proxy_id)
        active_backup = crud.get_proxy_by_id(session, backup_proxy_id)
        task = crud.get_registration_task_by_uuid(session, "task-proxy-failover")

        assert disabled_primary is not None
        assert disabled_primary.enabled is False
        assert active_backup is not None
        assert active_backup.enabled is True
        assert task is not None
        assert task.status == "completed"
        assert task.proxy == "http://127.0.0.1:8002"
