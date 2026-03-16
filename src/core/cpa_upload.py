"""
CPA (Codex Protocol API) 上传功能
"""

import json
import logging
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlMime

from ..database.session import get_db
from ..database.models import Account
from ..config.settings import get_settings

logger = logging.getLogger(__name__)


def generate_token_json(account: Account) -> dict:
    """
    生成 CPA 格式的 Token JSON

    Args:
        account: 账号模型实例

    Returns:
        CPA 格式的 Token 字典
    """
    return {
        "type": "codex",
        "email": account.email,
        "expired": account.expires_at.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.expires_at else "",
        "id_token": account.id_token or "",
        "account_id": account.account_id or "",
        "access_token": account.access_token or "",
        "last_refresh": account.last_refresh.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.last_refresh else "",
        "refresh_token": account.refresh_token or "",
    }


def upload_to_cpa(token_data: dict, proxy: str = None) -> Tuple[bool, str]:
    """
    上传单个账号到 CPA 管理平台（不走代理）

    Args:
        token_data: Token JSON 数据
        proxy: 保留参数，不使用（CPA 上传始终直连）

    Returns:
        (成功标志, 消息或错误信息)
    """
    settings = get_settings()

    if not settings.cpa_enabled:
        return False, "CPA 上传未启用"

    if not settings.cpa_api_url:
        return False, "CPA API URL 未配置"

    api_url = settings.cpa_api_url.rstrip("/")
    upload_url = f"{api_url}/v0/management/auth-files"

    filename = f"{token_data['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {settings.cpa_api_token.get_secret_value()}",
    }

    try:
        mime = CurlMime()
        mime.addpart(
            name="file",
            data=file_content,
            filename=filename,
            content_type="application/json",
        )

        response = cffi_requests.post(
            upload_url,
            multipart=mime,
            headers=headers,
            proxies=None,
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        error_msg = f"上传失败: HTTP {response.status_code}"
        try:
            error_detail = response.json()
            if isinstance(error_detail, dict):
                error_msg = error_detail.get("message", error_msg)
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"
        return False, error_msg

    except Exception as e:
        logger.error(f"CPA 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_cpa(account_ids: List[int], proxy: str = None) -> dict:
    """
    批量上传账号到 CPA 管理平台

    Args:
        account_ids: 账号 ID 列表
        proxy: 可选的代理 URL

    Returns:
        包含成功/失败统计和详情的字典
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": []
    }

    with get_db() as db:
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()

            if not account:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": None,
                    "success": False,
                    "error": "账号不存在"
                })
                continue

            # 检查是否已有 Token
            if not account.access_token:
                results["skipped_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": "缺少 Token"
                })
                continue

            # 生成 Token JSON
            token_data = generate_token_json(account)

            # 上传
            success, message = upload_to_cpa(token_data, proxy)

            if success:
                # 更新数据库状态
                account.cpa_uploaded = True
                account.cpa_uploaded_at = datetime.utcnow()
                db.commit()

                results["success_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": True,
                    "message": message
                })
            else:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": message
                })

    return results


def test_cpa_connection(api_url: str, api_token: str, proxy: str = None) -> Tuple[bool, str]:
    """
    测试 CPA 连接（不走代理）

    Args:
        api_url: CPA API URL
        api_token: CPA API Token
        proxy: 保留参数，不使用（CPA 始终直连）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"

    if not api_token:
        return False, "API Token 不能为空"

    api_url = api_url.rstrip("/")
    test_url = f"{api_url}/v0/management/auth-files"
    headers = {"Authorization": f"Bearer {api_token}"}

    try:
        response = cffi_requests.options(
            test_url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code in (200, 204, 401, 403, 405):
            if response.status_code == 401:
                return False, "连接成功，但 API Token 无效"
            return True, "CPA 连接测试成功"

        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
