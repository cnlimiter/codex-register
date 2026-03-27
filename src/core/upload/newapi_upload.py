"""
NEWAPI 上传功能 — 通过 PUT /api/channel/ 添加渠道
"""

import json
import logging
from datetime import datetime
from typing import List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL_TYPE = 1
DEFAULT_CHANNEL_BASE_URL = ""
DEFAULT_CHANNEL_MODELS = "gpt-5.4,gpt-5,gpt-5-codex,gpt-5-codex-mini,gpt-5.1,gpt-5.1-codex,gpt-5.1-codex-max,gpt-5.1-codex-mini,gpt-5.2,gpt-5.2-codex,gpt-5.3-codex,gpt-5-openai-compact,gpt-5-codex-openai-compact,gpt-5-codex-mini-openai-compact,gpt-5.1-openai-compact,gpt-5.1-codex-openai-compact,gpt-5.1-codex-max-openai-compact,gpt-5.1-codex-mini-openai-compact,gpt-5.2-openai-compact,gpt-5.2-codex-openai-compact,gpt-5.3-codex-openai-compact"


def _normalize_base(api_url: str) -> str:
    return (api_url or "").strip().rstrip("/")


def normalize_authorization_token(header_value: str, header_name: str = "Authorization Token") -> str:
    normalized_value = (header_value or "").strip()
    if not normalized_value:
        raise ValueError(f"{header_name} 不能为空")
    try:
        normalized_value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{header_name} 包含非 ASCII 字符，请确认填写的是实际令牌而不是中文说明") from exc
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized_value):
        raise ValueError(f"{header_name} 包含非法控制字符")
    return normalized_value


def _mask_header_value(header_value: str, keep: int = 4) -> str:
    """
    Mask a sensitive header value for safe logging.

    The strategy is:
    - If the value is empty, return an empty string.
    - If the length is <= keep, fully mask it (no characters revealed).
    - Otherwise, reveal only the last `keep` characters and mask the rest.
    """
    if not header_value:
        return ""

    length = len(header_value)
    if length <= keep:
        return "*" * length

    masked_prefix = "*" * (length - keep)
    visible_suffix = header_value[-keep:]
    return masked_prefix + visible_suffix
def _build_headers(api_key: str) -> dict:
    safe_api_key = normalize_authorization_token(api_key)
    return {
        "Authorization": f"Bearer {safe_api_key}",
        "New-Api-User": "1",
        "Content-Type": "application/json",
    }


def _extract_error(resp) -> str:
    error_msg = f"上传失败: HTTP {resp.status_code}"
    try:
        detail = resp.json()
        if isinstance(detail, dict):
            error_msg = detail.get("message", error_msg)
    except Exception:
        error_msg = f"{error_msg} - {resp.text[:200]}"
    return error_msg


def upload_to_newapi(
    account: Account,
    api_url: str,
    api_key: str,
    channel_type: Optional[int] = None,
    channel_base_url: Optional[str] = None,
    channel_models: Optional[str] = None,
) -> Tuple[bool, str]:
    base = _normalize_base(api_url)
    if not base:
        return False, "NEWAPI API URL 未配置"
    if not api_key:
        return False, "NEWAPI API Key 未配置"
    if not account.access_token:
        return False, "账号缺少 access_token"

    resolved_channel_type = channel_type if isinstance(channel_type, int) and channel_type > 0 else DEFAULT_CHANNEL_TYPE
    resolved_channel_base_url = (channel_base_url or DEFAULT_CHANNEL_BASE_URL).strip()
    resolved_channel_models = (channel_models or DEFAULT_CHANNEL_MODELS).strip() or DEFAULT_CHANNEL_MODELS

    url = f"{base}/api/channel/"
    account_name = account.email or ""
    channel = {
        "auto_ban": 1,
        "name": account.email or "",
        "type": resolved_channel_type,
        "key": json.dumps({"access_token": account.access_token or "", "account_id": account_name}, ensure_ascii=True),
        "base_url": resolved_channel_base_url,
        "models": resolved_channel_models,
        "multi_key_mode": "random",
        "group": "default",
        "groups": ["default"],
        "priority": 0,
        "weight": 0,
    }

    try:
        payload = json.dumps({"mode": "single", "channel": channel}, ensure_ascii=True)
        headers = _build_headers(api_key)
        headers["Content-Type"] = "application/json; charset=utf-8"

        logger.info("NEWAPI 上传 URL: %s", url)
        safe_headers = dict(headers)
        if "Authorization" in safe_headers:
            safe_headers["Authorization"] = "REDACTED"
        logger.debug("NEWAPI 请求头: %s", safe_headers)

        resp = cffi_requests.post(
            url,
            headers=headers,
            data=payload.encode("utf-8"),
            proxies=None,
            timeout=30,
            impersonate="chrome110",
        )
        if resp.status_code in (200, 201):
            return True, "上传成功"
        return False, _extract_error(resp)
    except Exception as e:
        logger.error("NEWAPI 上传异常: %s", e)
        return False, f"上传异常: {str(e)}"


def batch_upload_to_newapi(
    account_ids: List[int],
    api_url: str,
    api_key: str,
    channel_type: Optional[int] = None,
    channel_base_url: Optional[str] = None,
    channel_models: Optional[str] = None,
) -> dict:
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
    }

    with get_db() as db:
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                results["failed_count"] += 1
                results["details"].append({"id": account_id, "email": None, "success": False, "error": "账号不存在"})
                continue
            if not account.access_token:
                results["skipped_count"] += 1
                results["details"].append({"id": account_id, "email": account.email, "success": False, "error": "缺少 Token"})
                continue

            success, message = upload_to_newapi(
                account,
                api_url,
                api_key,
                channel_type=channel_type,
                channel_base_url=channel_base_url,
                channel_models=channel_models,
            )
            if success:
                account.newapi_uploaded = True
                account.newapi_uploaded_at = datetime.utcnow()
                db.commit()
                results["success_count"] += 1
                results["details"].append({"id": account_id, "email": account.email, "success": True, "message": message})
            else:
                results["failed_count"] += 1
                results["details"].append({"id": account_id, "email": account.email, "success": False, "error": message})

    return results
