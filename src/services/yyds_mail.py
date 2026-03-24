"""
YYDS Mail (215.im) 邮箱服务实现
兼容 YYDS Mail v1 API (https://maliapi.215.im/v1)
强制直连，不走代理
"""

import logging
import random
import re
import string
import time
import threading
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)

# 域名列表缓存（进程级，跨实例共享）
_domains_cache: List[str] = []
_domains_lock = threading.Lock()


class YydsMailService(BaseEmailService):
    """
    YYDS Mail (215.im) 临时邮箱服务
    API 文档: https://maliapi.215.im/v1
    强制直连，不走全局代理
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 YYDS Mail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: API 基础地址 (默认: https://maliapi.215.im/v1)
                - api_key: API Key (X-API-Key header)
                - default_domain: 指定域名，为空则自动拉取可用域名列表
                - timeout: 请求超时 (默认: 30)
                - max_retries: 最大重试次数 (默认: 3)
            name: 服务名称
        """
        super().__init__(EmailServiceType.YYDS_MAIL, name)

        default_config = {
            "base_url": "https://maliapi.215.im/v1",
            "api_key": "",
            "default_domain": "",
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")

        # 强制直连，proxy_url=None
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 内存缓存：email -> {email, service_id, token, created_at}
        self._email_cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _api_headers(self, bearer_token: str = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        api_key = (self.config.get("api_key") or "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    def _fetch_domains(self) -> List[str]:
        """获取可用域名列表（进程级缓存）"""
        global _domains_cache
        with _domains_lock:
            if _domains_cache:
                return list(_domains_cache)

        try:
            resp = self.http_client.get(
                f"{self.config['base_url']}/domains",
                headers=self._api_headers(),
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
            domains = []
            for d in raw:
                if isinstance(d, str):
                    domains.append(d)
                elif isinstance(d, dict):
                    name = d.get("domain") or d.get("name") or ""
                    if name:
                        domains.append(name)
            if domains:
                with _domains_lock:
                    _domains_cache = domains
            return domains
        except Exception as e:
            logger.warning(f"获取 YYDS Mail 域名列表失败: {e}")
            return []

    def _pick_domain(self) -> str:
        explicit = (self.config.get("default_domain") or "").strip().lstrip("@")
        if explicit:
            return explicit
        domains = self._fetch_domains()
        if not domains:
            raise EmailServiceError("无法获取 YYDS Mail 可用域名列表")
        return random.choice(domains)

    # ------------------------------------------------------------------
    # BaseEmailService 接口实现
    # ------------------------------------------------------------------

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建 YYDS Mail 临时邮箱

        Returns:
            - email: 邮箱地址
            - service_id: mail token（同 token）
            - token: mail token
            - created_at: 创建时间戳
        """
        try:
            domain = self._pick_domain()
            chars = string.ascii_lowercase + string.digits
            prefix = "".join(random.choice(chars) for _ in range(random.randint(8, 13)))

            resp = self.http_client.post(
                f"{self.config['base_url']}/accounts",
                headers=self._api_headers(),
                json={"address": prefix, "domain": domain},
            )

            if resp.status_code not in (200, 201):
                raise EmailServiceError(
                    f"创建邮箱失败: HTTP {resp.status_code} - {resp.text[:200]}"
                )

            body = resp.json()
            data = body.get("data", body) if isinstance(body, dict) else body
            email = str(data.get("address") or f"{prefix}@{domain}").strip()
            token = str(data.get("token") or "").strip()

            if not token:
                raise EmailServiceError(
                    f"创建邮箱成功但未返回 token，响应: {body}"
                )

            info = {
                "email": email,
                "service_id": token,
                "token": token,
                "created_at": time.time(),
            }
            self._email_cache[email] = info
            logger.info(f"成功创建 YYDS Mail 邮箱: {email}")
            self.update_status(True)
            return info

        except EmailServiceError:
            raise
        except Exception as e:
            self.update_status(False, e)
            raise EmailServiceError(f"创建 YYDS Mail 邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        轮询 YYDS Mail 收件箱，提取 OTP 验证码
        """
        token = email_id
        if not token:
            cached = self._email_cache.get(email)
            token = cached.get("token") if cached else None

        if not token:
            logger.warning(f"未找到邮箱 {email} 的 token，无法获取验证码")
            return None

        logger.info(f"等待 YYDS Mail 验证码: {email}")
        start_time = time.time()
        seen_ids: set = set()

        while time.time() - start_time < timeout:
            try:
                resp = self.http_client.get(
                    f"{self.config['base_url']}/messages",
                    headers=self._api_headers(bearer_token=token),
                )

                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                body = resp.json()
                if isinstance(body, list):
                    messages = body
                elif isinstance(body, dict):
                    data = body.get("data", body)
                    messages = data if isinstance(data, list) else (data.get("messages") or [])
                else:
                    messages = []

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue

                    msg_id = msg.get("id") or msg.get("_id") or ""
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    message_marker = f"id:{msg_id}"

                    if self._is_message_before_otp(
                        msg.get("createdAt") or msg.get("created_at") or msg.get("date"),
                        otp_sent_at,
                    ):
                        continue

                    # 拉取邮件详情（含 HTML body）
                    detail = self._fetch_message_detail(token, msg_id)
                    content = self._extract_content(msg, detail)

                    if "openai" not in content.lower():
                        continue

                    match = re.search(pattern, content)
                    if match:
                        code = match.group(1)
                        if not self._accept_verification_code(email, code, message_marker):
                            continue
                        logger.info(f"YYDS Mail 验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"YYDS Mail 轮询出错: {e}")

            time.sleep(3)

        logger.warning(f"YYDS Mail 等待验证码超时: {email}")
        return None

    def _fetch_message_detail(self, token: str, msg_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.http_client.get(
                f"{self.config['base_url']}/messages/{msg_id}",
                headers=self._api_headers(bearer_token=token),
            )
            if resp.status_code == 200:
                body = resp.json()
                return body.get("data", body) if isinstance(body, dict) else body
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_content(msg: Dict[str, Any], detail: Optional[Dict[str, Any]]) -> str:
        parts = [
            str(msg.get("from") or ""),
            str(msg.get("subject") or ""),
        ]
        src = detail or msg
        parts.append(str(src.get("text") or src.get("body") or ""))
        parts.append(str(src.get("html") or ""))
        return "\n".join(parts)

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        to_del = [
            addr for addr, info in self._email_cache.items()
            if info.get("token") == email_id
        ]
        for addr in to_del:
            del self._email_cache[addr]
        return bool(to_del)

    def check_health(self) -> bool:
        try:
            resp = self.http_client.get(
                f"{self.config['base_url']}/domains",
                headers=self._api_headers(),
                timeout=10,
            )
            ok = resp.status_code == 200
            self.update_status(ok)
            return ok
        except Exception as e:
            logger.warning(f"YYDS Mail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
