"""
Tempmail.lol 邮箱服务实现
"""

import re
import time
import logging
from typing import Callable, Optional, Dict, Any, List
from datetime import datetime, timezone

from .base import BaseEmailService, EmailServiceError, EmailServiceType, OTPNoOpenAISenderEmailServiceError, get_email_code_settings
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN


logger = logging.getLogger(__name__)

OTP_SENT_AT_TOLERANCE_SECONDS = 2


class TempmailService(BaseEmailService):
    """
    Tempmail.lol 邮箱服务
    基于 Tempmail.lol API v2
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Tempmail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: API 基础地址 (默认: https://api.tempmail.lol/v2)
                - timeout: 请求超时时间 (默认: 30)
                - max_retries: 最大重试次数 (默认: 3)
                - proxy_url: 代理 URL
            name: 服务名称
        """
        super().__init__(EmailServiceType.TEMPMAIL, name)

        # 默认配置
        default_config = {
            "base_url": "https://api.tempmail.lol/v2",
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        # 创建 HTTP 客户端
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config
        )

        # 状态变量（内存缓存，重启后从 DB 按需查询）
        self._email_cache: Dict[str, Dict[str, Any]] = {}
        self._last_check_time: float = 0

    def _parse_message_time(self, value: Any) -> Optional[float]:
        """解析 Tempmail 邮件时间，兼容 Unix 时间戳与 ISO 8601。"""
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
                try:
                    normalized = text.replace("Z", "+00:00")
                    timestamp = datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
                except Exception:
                    return None

        while timestamp > 1e11:
            timestamp /= 1000.0
        return timestamp if timestamp > 0 else None

    def _get_received_timestamp(self, message: Dict[str, Any]) -> Optional[float]:
        """返回 Tempmail 邮件的接收时间戳。"""
        for field_name in ("received_at", "date", "created_at", "createdAt", "timestamp"):
            timestamp = self._parse_message_time(message.get(field_name))
            if timestamp is not None:
                return timestamp
        return None

    def _save_token_to_db(self, email: str, token: str) -> None:
        """将邮箱 token 持久化到 Setting 表，key=tempmail_token:{email}"""
        try:
            from ..database.session import get_db
            from ..database.crud import set_setting
            with get_db() as db:
                set_setting(db, f"tempmail_token:{email}", token, category="tempmail")
        except Exception as e:
            logger.warning(f"保存 Tempmail token 到数据库失败: {e}")

    def _load_token_from_db(self, email: str) -> Optional[str]:
        """从 Setting 表读取邮箱 token"""
        try:
            from ..database.session import get_db
            from ..database.crud import get_setting
            with get_db() as db:
                setting = get_setting(db, f"tempmail_token:{email}")
                return setting.value if setting else None
        except Exception as e:
            logger.warning(f"从数据库读取 Tempmail token 失败: {e}")
            return None

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建新的临时邮箱

        Args:
            config: 配置参数（Tempmail.lol 目前不支持自定义配置）

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 邮箱 token
            - token: 邮箱 token（同 service_id）
            - created_at: 创建时间戳
        """
        try:
            # 发送创建请求
            response = self.http_client.post(
                f"{self.config['base_url']}/inbox/create",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={}
            )

            if response.status_code not in (200, 201):
                self.update_status(False, EmailServiceError(f"请求失败，状态码: {response.status_code}"))
                raise EmailServiceError(f"Tempmail.lol 请求失败，状态码: {response.status_code}")

            data = response.json()
            email = str(data.get("address", "")).strip()
            token = str(data.get("token", "")).strip()

            if not email or not token:
                self.update_status(False, EmailServiceError("返回数据不完整"))
                raise EmailServiceError("Tempmail.lol 返回数据不完整")

            # 缓存邮箱信息
            email_info = {
                "email": email,
                "service_id": token,
                "token": token,
                "created_at": time.time(),
            }
            self._email_cache[email] = email_info
            self._save_token_to_db(email, token)

            logger.info(f"成功创建 Tempmail.lol 邮箱: {email}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建 Tempmail.lol 邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Tempmail.lol 获取验证码

        Args:
            email: 邮箱地址
            email_id: 邮箱 token（如果不提供，从缓存中查找）
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式
            otp_sent_at: OTP 发送时间戳，只允许使用严格晚于该锚点减去容差后的邮件

        Returns:
            验证码字符串，如果超时或未找到返回 None
        """
        token = email_id
        if not token:
            # 先从内存缓存查找，再从数据库查找
            if email in self._email_cache:
                token = self._email_cache[email].get("token")
            if not token:
                token = self._load_token_from_db(email)
                if not token:
                    logger.warning(f"未找到邮箱 {email} 的 token，无法获取验证码")
                    return None

        if not token:
            logger.warning(f"邮箱 {email} 没有 token，无法获取验证码")
            return None

        logger.info(f"正在等待邮箱 {email} 的验证码...")

        poll_interval = get_email_code_settings()["poll_interval"]
        start_time = time.time()
        seen_ids = set()

        while time.time() - start_time < timeout:
            try:
                # 获取邮件列表
                response = self.http_client.get(
                    f"{self.config['base_url']}/inbox",
                    params={"token": token},
                    headers={"Accept": "application/json"}
                )

                if response.status_code != 200:
                    time.sleep(poll_interval)
                    continue

                data = response.json()

                # 检查 inbox 是否过期
                if data is None or (isinstance(data, dict) and not data):
                    logger.warning(f"邮箱 {email} 已过期")
                    return None

                email_list = data.get("emails", []) if isinstance(data, dict) else []

                if not isinstance(email_list, list):
                    time.sleep(poll_interval)
                    continue

                ordered_emails = self._sort_items_by_message_time(
                    email_list,
                    lambda item: item.get("date") if isinstance(item, dict) else None,
                )

                if ordered_emails:
                    if not self._batch_has_openai_sender(
                        ordered_emails,
                        lambda item: item.get("from") if isinstance(item, dict) else None,
                    ):
                        raise OTPNoOpenAISenderEmailServiceError()

                for msg in ordered_emails:
                    if not isinstance(msg, dict):
                        continue

                    msg_timestamp = self._get_received_timestamp(msg)
                    if otp_sent_at is not None:
                        min_allowed_timestamp = otp_sent_at - OTP_SENT_AT_TOLERANCE_SECONDS
                        if msg_timestamp is None or msg_timestamp <= min_allowed_timestamp:
                            continue

                    message_id = str(
                        msg.get("id")
                        or msg.get("date")
                        or msg.get("createdAt")
                        or f"{msg.get('from', '')}:{msg.get('subject', '')}:{msg_timestamp}"
                    ).strip()
                    if not message_id or message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)
                    message_marker = f"id:{message_id}"

                    sender = str(msg.get("from", "")).lower()
                    subject = str(msg.get("subject", ""))
                    body = str(msg.get("body", ""))
                    html = str(msg.get("html") or "")

                    content = "\n".join([sender, subject, body, html])

                    # 检查是否是 OpenAI 邮件
                    if not self._is_openai_candidate_message(sender, subject, body, html):
                        continue

                    # 提取验证码
                    match = re.search(pattern, content)
                    if match:
                        code = match.group(1)
                        if not self._accept_verification_code(email, code, message_marker):
                            continue
                        logger.info(f"找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                if isinstance(e, OTPNoOpenAISenderEmailServiceError):
                    raise
                logger.debug(f"检查邮件时出错: {e}")

            # 等待一段时间再检查
            time.sleep(poll_interval)

        logger.warning(f"等待验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出所有缓存的邮箱

        Note:
            Tempmail.lol API 不支持列出所有邮箱，这里返回缓存的邮箱
        """
        return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Note:
            Tempmail.lol API 不支持删除邮箱，这里从缓存中移除
        """
        # 从缓存中查找并移除
        emails_to_delete = []
        for email, info in self._email_cache.items():
            if info.get("token") == email_id:
                emails_to_delete.append(email)

        for email in emails_to_delete:
            del self._email_cache[email]
            logger.info(f"从缓存中移除邮箱: {email}")

        return len(emails_to_delete) > 0

    def check_health(self) -> bool:
        """检查 Tempmail.lol 服务是否可用"""
        try:
            response = self.http_client.get(
                f"{self.config['base_url']}/inbox/create",
                timeout=10
            )
            # 即使返回错误状态码也认为服务可用（只要可以连接）
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Tempmail.lol 健康检查失败: {e}")
            self.update_status(False, e)
            return False

    def get_inbox(self, token: str) -> Optional[Dict[str, Any]]:
        """
        获取邮箱收件箱内容

        Args:
            token: 邮箱 token

        Returns:
            收件箱数据
        """
        try:
            response = self.http_client.get(
                f"{self.config['base_url']}/inbox",
                params={"token": token},
                headers={"Accept": "application/json"}
            )

            if response.status_code != 200:
                return None

            return response.json()
        except Exception as e:
            logger.error(f"获取收件箱失败: {e}")
            return None

    def wait_for_verification_code_with_callback(
        self,
        email: str,
        token: str,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: int = 120
    ) -> Optional[str]:
        """
        等待验证码并支持回调函数

        Args:
            email: 邮箱地址
            token: 邮箱 token
            callback: 回调函数，接收当前状态信息
            timeout: 超时时间

        Returns:
            验证码或 None
        """
        poll_interval = get_email_code_settings()["poll_interval"]
        start_time = time.time()
        seen_ids = set()
        check_count = 0

        while time.time() - start_time < timeout:
            check_count += 1

            if callback:
                callback({
                    "status": "checking",
                    "email": email,
                    "check_count": check_count,
                    "elapsed_time": time.time() - start_time,
                })

            try:
                data = self.get_inbox(token)
                if not data:
                    time.sleep(poll_interval)
                    continue

                # 检查 inbox 是否过期
                if data is None or (isinstance(data, dict) and not data):
                    if callback:
                        callback({
                            "status": "expired",
                            "email": email,
                            "message": "邮箱已过期"
                        })
                    return None

                email_list = data.get("emails", []) if isinstance(data, dict) else []

                for msg in email_list:
                    msg_date = msg.get("date", 0)
                    if not msg_date or msg_date in seen_ids:
                        continue
                    seen_ids.add(msg_date)

                    sender = str(msg.get("from", "")).lower()
                    subject = str(msg.get("subject", ""))
                    body = str(msg.get("body", ""))
                    html = str(msg.get("html") or "")

                    content = "\n".join([sender, subject, body, html])

                    # 检查是否是 OpenAI 邮件
                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    # 提取验证码
                    match = re.search(OTP_CODE_PATTERN, content)
                    if match:
                        code = match.group(1)
                        if callback:
                            callback({
                                "status": "found",
                                "email": email,
                                "code": code,
                                "message": "找到验证码"
                            })
                        return code

                if callback and check_count % 5 == 0:
                    callback({
                        "status": "waiting",
                        "email": email,
                        "check_count": check_count,
                        "message": f"已检查 {len(seen_ids)} 封邮件，等待验证码..."
                    })

            except Exception as e:
                logger.debug(f"检查邮件时出错: {e}")
                if callback:
                    callback({
                        "status": "error",
                        "email": email,
                        "error": str(e),
                        "message": "检查邮件时出错"
                    })

            time.sleep(poll_interval)

        if callback:
            callback({
                "status": "timeout",
                "email": email,
                "message": "等待验证码超时"
            })
        return None
