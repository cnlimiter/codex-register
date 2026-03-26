import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None
    tempmail_base_url = "https://api.tempmail.lol/v2"
    tempmail_timeout = 30
    tempmail_max_retries = 3


def test_cloud_mail_service_registered():
    service_type = EmailServiceType("cloud_mail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "CloudMailService"


def test_email_service_types_include_cloud_mail():
    result = asyncio.run(email_routes.get_service_types())
    cloud_mail_type = next(item for item in result["types"] if item["value"] == "cloud_mail")

    assert cloud_mail_type["label"] == "Cloud Mail"
    field_names = [field["name"] for field in cloud_mail_type["config_fields"]]
    assert "base_url" in field_names
    assert "admin_email" in field_names
    assert "admin_password" in field_names
    assert "default_domain" in field_names


def test_filter_sensitive_config_marks_cloud_mail_admin_password():
    filtered = email_routes.filter_sensitive_config({
        "base_url": "https://mail.example.com",
        "admin_email": "admin@example.com",
        "admin_password": "admin-secret",
        "default_domain": "mail.example.com",
    })

    assert filtered["base_url"] == "https://mail.example.com"
    assert filtered["admin_email"] == "admin@example.com"
    assert filtered["default_domain"] == "mail.example.com"
    assert filtered["has_admin_password"] is True
    assert "admin_password" not in filtered


def test_registration_available_services_include_cloud_mail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "cloudmail_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="cloud_mail",
                name="Cloud Mail 主服务",
                config={
                    "base_url": "https://mail.example.com",
                    "admin_email": "admin@example.com",
                    "admin_password": "admin-secret",
                    "default_domain": "mail.example.com",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["cloud_mail"]["available"] is True
    assert result["cloud_mail"]["count"] == 1
    assert result["cloud_mail"]["services"][0]["name"] == "Cloud Mail 主服务"
    assert result["cloud_mail"]["services"][0]["type"] == "cloud_mail"
    assert result["cloud_mail"]["services"][0]["default_domain"] == "mail.example.com"


def test_normalize_email_service_config_maps_cloud_mail_domain_alias():
    config = registration_routes._normalize_email_service_config(
        EmailServiceType("cloud_mail"),
        {
            "base_url": "https://mail.example.com",
            "admin_email": "admin@example.com",
            "admin_password": "admin-secret",
            "domain": "mail.example.com",
        },
        proxy_url=None,
    )

    assert config["default_domain"] == "mail.example.com"
    assert "domain" not in config
