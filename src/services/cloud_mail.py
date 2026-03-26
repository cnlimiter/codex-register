"""
Cloud Mail 邮箱服务实现
基于 maillab/cloud-mail 的 public API
"""

import logging
import random
import re
import string
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)

OTP_SENT_AT_TOLERANCE_SECONDS = 2


class CloudMailService(BaseEmailService):
    """Cloud Mail 邮箱服务"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.CLOUD_MAIL, name)

        required_keys = ["base_url", "admin_email", "admin_password", "default_domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
            "password_length": 16,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")
        self.config["default_domain"] = str(self.config["default_domain"]).strip().lstrip("@")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        self._email_cache: Dict[str, Dict[str, Any]] = {}

    def _build_headers(
        self,
        token: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = token
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _unwrap_result(self, payload: Any) -> Any:
        if not isinstance(payload, dict) or "code" not in payload:
            return payload

        if payload.get("code") != 200:
            raise EmailServiceError(str(payload.get("message") or "Cloud Mail API 返回失败"))

        return payload.get("data")

    def _make_request(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        **kwargs,
    ) -> Any:
        url = f"{self.config['base_url']}/api{path}"
        kwargs["headers"] = self._build_headers(token=token, extra_headers=kwargs.get("headers"))

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                retry_after = None
                if response.status_code == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    if retry_after_header:
                        try:
                            retry_after = max(1, int(retry_after_header))
                        except ValueError:
                            retry_after = None
                    if retry_after is not None:
                        error_msg = f"{error_msg} (Retry-After: {retry_after}s)"
                error = EmailServiceError(error_msg)
                self.update_status(False, error)
                raise error

            try:
                payload = response.json()
            except Exception:
                payload = {"raw_response": response.text}

            data = self._unwrap_result(payload)
            return data
        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _get_public_token(self) -> str:
        data = self._make_request(
            "POST",
            "/public/genToken",
            json={
                "email": self.config["admin_email"],
                "password": self.config["admin_password"],
            },
        )

        if isinstance(data, dict):
            token = str(data.get("token") or "").strip()
        else:
            token = str(data or "").strip()

        if not token:
            raise EmailServiceError("Cloud Mail 未返回 public token")

        return token

    def _generate_local_part(self) -> str:
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
        return f"{first}{rest}"

    def _generate_password(self) -> str:
        length = max(8, int(self.config.get("password_length") or 16))
        alphabet = string.ascii_letters + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _parse_message_time(self, value: Any) -> Optional[float]:
        if value is None or value == "":
            return None

        if isinstance(value, (int, float)):
            timestamp = float(value)
        else:
            text = str(value).strip()
            if not text:
                return None

            try:
                timestamp = float(text)
            except ValueError:
                normalized = text.replace("Z", "+00:00")
                if "T" not in normalized and "+" not in normalized[10:] and normalized.count(":") >= 2:
                    normalized = normalized.replace(" ", "T", 1) + "+00:00"
                try:
                    parsed = datetime.fromisoformat(normalized)
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.astimezone(timezone.utc).timestamp()

        while timestamp > 1e11:
            timestamp /= 1000.0
        return timestamp if timestamp > 0 else None

    def _get_received_timestamp(self, mail: Dict[str, Any]) -> Optional[float]:
        for field_name in ("createTime", "createdAt", "receivedAt", "timestamp", "time"):
            timestamp = self._parse_message_time(mail.get(field_name))
            if timestamp is not None:
                return timestamp
        return None

    def _extract_otp(self, text: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, text or "")
        if not match:
            return None
        return match.group(1)

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = config or {}
        local_part = str(request_config.get("name") or self._generate_local_part()).strip()
        domain = str(
            request_config.get("default_domain")
            or request_config.get("domain")
            or self.config["default_domain"]
        ).strip().lstrip("@")
        address = f"{local_part}@{domain}"
        password = str(request_config.get("password") or self._generate_password())

        token = self._get_public_token()
        self._make_request(
            "POST",
            "/public/addUser",
            token=token,
            json={
                "list": [{
                    "email": address,
                    "password": password,
                }]
            },
        )

        email_info = {
            "email": address,
            "password": password,
            "service_id": address,
            "id": address,
            "created_at": time.time(),
        }
        self._email_cache[address.lower()] = email_info
        self.update_status(True)
        logger.info(f"成功创建 Cloud Mail 邮箱: {address}")
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        logger.info(f"正在从 Cloud Mail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()

        while time.time() - start_time < timeout:
            try:
                token = self._get_public_token()
                mails = self._make_request(
                    "POST",
                    "/public/emailList",
                    token=token,
                    json={
                        "toEmail": email,
                        "num": 1,
                        "size": 20,
                    },
                )

                if isinstance(mails, dict) and isinstance(mails.get("list"), list):
                    mails = mails["list"]

                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                for mail in mails:
                    msg_timestamp = self._get_received_timestamp(mail)
                    if otp_sent_at is not None:
                        min_allowed_timestamp = otp_sent_at - OTP_SENT_AT_TOLERANCE_SECONDS
                        if msg_timestamp is None or msg_timestamp <= min_allowed_timestamp:
                            continue

                    mail_id = mail.get("emailId") or mail.get("id")
                    if mail_id in seen_mail_ids:
                        continue
                    if mail_id is not None:
                        seen_mail_ids.add(mail_id)

                    sender = str(mail.get("sendEmail") or mail.get("sender") or "")
                    sender_name = str(mail.get("sendName") or mail.get("name") or "")
                    subject = str(mail.get("subject") or "")
                    text_body = str(mail.get("text") or "")
                    content = str(mail.get("content") or "")
                    search_text = "\n".join(
                        part for part in [sender, sender_name, subject, text_body, content] if part
                    ).strip()

                    if "openai" not in search_text.lower():
                        continue

                    code = self._extract_otp(search_text, pattern)
                    if code:
                        message_marker = f"id:{mail_id}" if mail_id is not None else None
                        if not self._accept_verification_code(email, code, message_marker):
                            continue
                        self.update_status(True)
                        logger.info(f"从 Cloud Mail 邮箱 {email} 找到验证码: {code}")
                        return code
            except Exception as e:
                logger.debug(f"检查 Cloud Mail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 Cloud Mail 验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        self._email_cache.pop(str(email_id).strip().lower(), None)
        self.update_status(True)
        return True

    def check_health(self) -> bool:
        try:
            self._get_public_token()
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Cloud Mail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
