from src.database import crud
from src.database.session import DatabaseSessionManager


def test_create_account_marks_token_sync_pending_when_tokens_persist(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/test.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="sync@example.com",
            email_service="tempmail",
            access_token="access-token",
            refresh_token="refresh-token",
        )

        assert account.token_sync_status == "pending"
        assert account.token_sync_updated_at is not None


def test_update_account_marks_token_sync_pending_when_tokens_change(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path}/test.db")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as session:
        account = crud.create_account(
            session,
            email="nosync@example.com",
            email_service="tempmail",
        )

        assert account.token_sync_status == "not_ready"

        updated = crud.update_account(
            session,
            account.id,
            access_token="new-access-token",
        )

        assert updated is not None
        assert updated.token_sync_status == "pending"
        assert updated.token_sync_updated_at is not None
