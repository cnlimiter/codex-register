"""
邮箱访问凭证快照与恢复辅助函数
"""

from typing import Any, Dict, Optional

from ..config.constants import EmailServiceType


EMAIL_ACCESS_KEY = "email_access"


def _normalize_service_type(service_type: EmailServiceType | str) -> EmailServiceType:
    if isinstance(service_type, EmailServiceType):
        return service_type
    return EmailServiceType(service_type)


def _clean_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned


def _resolve_outlook_config(email: str, service_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = service_config or {}

    if "accounts" in config and isinstance(config["accounts"], list):
        email_lower = str(email or "").strip().lower()
        for account in config["accounts"]:
            account_email = str((account or {}).get("email") or "").strip().lower()
            if account_email == email_lower:
                return account or {}
        return {}

    return config


def build_email_access_snapshot(
    service_type: EmailServiceType | str,
    email: str,
    email_info: Optional[Dict[str, Any]] = None,
    service_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    提取账号后续再次收件或人工登录邮箱所需的最小凭证快照。
    """
    normalized_type = _normalize_service_type(service_type)
    snapshot: Dict[str, Any] = {
        "service_type": normalized_type.value,
        "email": email,
    }
    info = email_info or {}

    if normalized_type == EmailServiceType.DUCK_MAIL:
        snapshot.update({
            "service_id": info.get("service_id"),
            "account_id": info.get("account_id") or info.get("service_id"),
            "password": info.get("password"),
            "token": info.get("token"),
        })
    elif normalized_type == EmailServiceType.TEMPMAIL:
        snapshot.update({
            "service_id": info.get("service_id"),
            "token": info.get("token") or info.get("service_id"),
        })
    elif normalized_type == EmailServiceType.TEMP_MAIL:
        snapshot.update({
            "service_id": info.get("service_id"),
            "jwt": info.get("jwt"),
        })
    elif normalized_type == EmailServiceType.IMAP_MAIL:
        config = service_config or {}
        snapshot.update({
            "email": config.get("email") or email,
            "password": config.get("password"),
            "host": config.get("host"),
            "port": config.get("port"),
            "use_ssl": config.get("use_ssl"),
        })
    elif normalized_type == EmailServiceType.OUTLOOK:
        config = _resolve_outlook_config(email, service_config)
        snapshot.update({
            "email": config.get("email") or email,
            "password": config.get("password"),
            "client_id": config.get("client_id"),
            "refresh_token": config.get("refresh_token"),
        })
    else:
        snapshot.update({
            "service_id": info.get("service_id"),
        })

    return _clean_dict(snapshot)


def get_email_access_snapshot(extra_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    access = (extra_data or {}).get(EMAIL_ACCESS_KEY)
    if isinstance(access, dict):
        return access
    return {}


def inject_email_access_config(
    service_type: EmailServiceType | str,
    base_config: Optional[Dict[str, Any]],
    email_access: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    将账户级邮箱凭证注入服务配置，便于重建服务实例后继续查询验证码。
    """
    normalized_type = _normalize_service_type(service_type)
    config = dict(base_config or {})
    access = _clean_dict(email_access)
    if not access:
        return config

    if normalized_type in {EmailServiceType.DUCK_MAIL, EmailServiceType.TEMPMAIL, EmailServiceType.TEMP_MAIL}:
        preloaded = list(config.get("preloaded_accounts") or [])
        preloaded.append(access)
        config["preloaded_accounts"] = preloaded
        return config

    if normalized_type == EmailServiceType.IMAP_MAIL:
        for key in ("email", "password", "host", "port", "use_ssl"):
            if key in access:
                config[key] = access[key]
        return config

    if normalized_type == EmailServiceType.OUTLOOK:
        account_config = _clean_dict({
            "email": access.get("email"),
            "password": access.get("password"),
            "client_id": access.get("client_id"),
            "refresh_token": access.get("refresh_token"),
        })
        if account_config:
            config.update(account_config)
            config["accounts"] = [account_config]
        return config

    return config
