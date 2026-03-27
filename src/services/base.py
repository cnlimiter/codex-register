"""
邮箱服务抽象基类
所有邮箱服务实现的基类
"""

import abc
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum

from ..config.constants import EmailServiceType, OPENAI_EMAIL_SENDERS, OTP_CODE_PATTERN, OTP_CODE_SEMANTIC_PATTERN
from ..config.settings import get_settings


logger = logging.getLogger(__name__)

EMAIL_PROVIDER_BACKOFF_BASE_SECONDS = 30


def get_email_code_settings() -> dict:
    """获取验证码等待配置（timeout、poll_interval）"""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }
EMAIL_PROVIDER_BACKOFF_MAX_SECONDS = 3600
OTP_TIMEOUT_ERROR_PREFIX = "OTP_TIMEOUT"
OTP_NO_OPENAI_SENDER_ERROR = "OTP_NO_OPENAI_SENDER"


@dataclass(frozen=True)
class EmailProviderBackoffState:
    """邮箱供应商退避状态"""

    failures: int = 0
    delay_seconds: int = 0
    opened_until: float = 0.0
    retry_after: Optional[int] = None
    last_error: Optional[str] = None

    def is_open(self, now: Optional[float] = None) -> bool:
        now_ts = now if now is not None else time.time()
        return self.opened_until > now_ts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failures": self.failures,
            "delay_seconds": self.delay_seconds,
            "opened_until": self.opened_until,
            "retry_after": self.retry_after,
            "last_error": self.last_error,
        }


def calculate_adaptive_backoff_delay(
    failures: int,
    base_delay: int = EMAIL_PROVIDER_BACKOFF_BASE_SECONDS,
    max_delay: int = EMAIL_PROVIDER_BACKOFF_MAX_SECONDS,
    is_timeout: bool = False,
) -> int:
    """根据连续失败次数计算指数退避时长"""
    normalized_failures = max(0, failures)
    if is_timeout and normalized_failures >= 3:
        return max_delay
    exponent = max(0, normalized_failures - 1)
    return min(base_delay * (2 ** exponent), max_delay)


def is_otp_timeout_error(error: object) -> bool:
    """识别 OTP 超时类错误码。"""
    if error is None:
        return False
    if isinstance(error, OTPTimeoutEmailServiceError):
        return True
    error_code = getattr(error, "error_code", "")
    if isinstance(error_code, str) and error_code.startswith(OTP_TIMEOUT_ERROR_PREFIX):
        return True
    return False


def apply_adaptive_backoff(
    current_state: Optional[EmailProviderBackoffState],
    error: "EmailServiceError",
    now: Optional[float] = None,
) -> EmailProviderBackoffState:
    """在限流场景下推进邮箱供应商退避状态"""
    state = current_state or EmailProviderBackoffState()
    now_ts = now if now is not None else time.time()
    next_failures = state.failures + 1
    delay_seconds = calculate_adaptive_backoff_delay(
        next_failures,
        is_timeout=is_otp_timeout_error(error),
    )
    return EmailProviderBackoffState(
        failures=next_failures,
        delay_seconds=delay_seconds,
        opened_until=now_ts + delay_seconds,
        retry_after=getattr(error, "retry_after", None),
        last_error=str(error),
    )


def reset_adaptive_backoff() -> EmailProviderBackoffState:
    """重置邮箱供应商退避状态"""
    return EmailProviderBackoffState()


class EmailServiceError(Exception):
    """邮箱服务异常"""
    pass


class RateLimitedEmailServiceError(EmailServiceError):
    """邮箱服务被限流"""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class OTPTimeoutEmailServiceError(EmailServiceError):
    """OTP 验证码等待超时。"""

    def __init__(self, message: str, error_code: str = OTP_TIMEOUT_ERROR_PREFIX):
        super().__init__(message)
        self.error_code = error_code


class OTPNoOpenAISenderEmailServiceError(EmailServiceError):
    """当前轮询批次未发现 OpenAI 发件人，建议立即重发验证码。"""

    def __init__(self, message: str = "当前邮件批次未发现 OpenAI 发件人", error_code: str = OTP_NO_OPENAI_SENDER_ERROR):
        super().__init__(message)
        self.error_code = error_code


class EmailServiceStatus(Enum):
    """邮箱服务状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class BaseEmailService(abc.ABC):
    """
    邮箱服务抽象基类

    所有邮箱服务必须实现此接口
    """

    def __init__(self, service_type: EmailServiceType, name: str = None):
        """
        初始化邮箱服务

        Args:
            service_type: 服务类型
            name: 服务名称
        """
        self.service_type = service_type
        self.name = name or f"{service_type.value}_service"
        self._status = EmailServiceStatus.HEALTHY
        self._last_error = None
        self._provider_backoff = reset_adaptive_backoff()
        self._used_verification_codes: Dict[str, set] = {}
        self._seen_verification_messages: Dict[str, set] = {}

    _EMAIL_ADDRESS_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    @property
    def status(self) -> EmailServiceStatus:
        """获取服务状态"""
        return self._status

    @property
    def last_error(self) -> Optional[str]:
        """获取最后一次错误信息"""
        return self._last_error

    @property
    def provider_backoff_state(self) -> EmailProviderBackoffState:
        """获取当前邮箱供应商退避状态"""
        return self._provider_backoff

    def apply_provider_backoff_state(self, state: Optional[EmailProviderBackoffState]) -> None:
        """注入外部持久化的邮箱供应商退避状态"""
        self._provider_backoff = state or reset_adaptive_backoff()

    @abc.abstractmethod
    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建新邮箱地址

        Args:
            config: 配置参数，如邮箱前缀、域名等

        Returns:
            包含邮箱信息的字典，至少包含:
            - email: 邮箱地址
            - service_id: 邮箱服务中的 ID
            - token/credentials: 访问凭证（如果需要）

        Raises:
            EmailServiceError: 创建失败
        """
        pass

    @abc.abstractmethod
    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        获取验证码

        Args:
            email: 邮箱地址
            email_id: 邮箱服务中的 ID（如果需要）
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式
            otp_sent_at: OTP 发送时间戳，只允许使用严格晚于该锚点的邮件

        Returns:
            验证码字符串，如果超时或未找到返回 None

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出所有邮箱（如果服务支持）

        Args:
            **kwargs: 其他参数

        Returns:
            邮箱列表

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Args:
            email_id: 邮箱服务中的 ID

        Returns:
            是否删除成功

        Raises:
            EmailServiceError: 服务错误
        """
        pass

    @abc.abstractmethod
    def check_health(self) -> bool:
        """
        检查服务健康状态

        Returns:
            服务是否健康

        Note:
            此方法不应抛出异常，应捕获异常并返回 False
        """
        pass

    def get_email_info(self, email_id: str) -> Optional[Dict[str, Any]]:
        """
        获取邮箱信息（可选实现）

        Args:
            email_id: 邮箱服务中的 ID

        Returns:
            邮箱信息字典，如果不存在返回 None
        """
        # 默认实现：遍历列表查找
        for email_info in self.list_emails():
            if email_info.get("id") == email_id:
                return email_info
        return None

    def _strip_email_addresses(self, text: str) -> str:
        """移除文本中的邮箱地址，避免域名数字被误识别为验证码。"""
        return self._EMAIL_ADDRESS_PATTERN.sub(" ", text or "")

    def _extract_otp_from_text(self, text: str, pattern: Optional[str] = None) -> Optional[str]:
        """
        从文本中提取验证码。

        优先语义匹配，再在移除邮箱地址后的文本上做 6 位数字兜底。
        """
        if not text:
            return None

        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1)

        fallback_pattern = pattern or OTP_CODE_PATTERN
        simple_match = re.search(fallback_pattern, self._strip_email_addresses(text))
        if simple_match:
            return simple_match.group(1)

        return None

    def _is_openai_sender_value(self, sender: Any) -> bool:
        """判断单个发件人字段是否属于 OpenAI。"""
        sender_text = str(sender or "").strip().lower()
        if not sender_text:
            return False

        for known_sender in OPENAI_EMAIL_SENDERS:
            normalized = known_sender.lower()
            if normalized.startswith(("@", ".")):
                if normalized in sender_text:
                    return True
            elif normalized in sender_text:
                return True
        return False

    def _message_mentions_openai(self, *parts: Any) -> bool:
        """判断若干文本片段中是否提及 OpenAI。"""
        combined = "\n".join(str(part or "") for part in parts if part is not None).lower()
        return "openai" in combined if combined else False

    def _is_openai_candidate_message(self, sender: Any = None, *content_parts: Any) -> bool:
        """判断单封邮件是否可作为 OpenAI 验证码候选邮件。"""
        return self._is_openai_sender_value(sender) or self._message_mentions_openai(sender, *content_parts)

    def _batch_has_openai_sender(self, items: List[Any], sender_getter) -> bool:
        """判断当前批次邮件是否至少有一封来自 OpenAI 发件人。"""
        found_sender_field = False
        for item in items:
            sender = sender_getter(item)
            if sender in (None, ""):
                continue
            found_sender_field = True
            if self._is_openai_sender_value(sender):
                return True
        return not found_sender_field

    def _get_used_verification_codes(self, email: str) -> set:
        """获取邮箱对应的已使用验证码集合。"""
        key = str(email or "").strip().lower()
        if key not in self._used_verification_codes:
            self._used_verification_codes[key] = set()
        return self._used_verification_codes[key]

    def _get_seen_verification_messages(self, email: str) -> set:
        """获取邮箱对应的已处理消息标识集合。"""
        key = str(email or "").strip().lower()
        if key not in self._seen_verification_messages:
            self._seen_verification_messages[key] = set()
        return self._seen_verification_messages[key]

    def load_verification_state(
        self,
        email: str,
        used_codes: Optional[List[str]] = None,
        seen_messages: Optional[List[str]] = None,
    ) -> None:
        """将持久化的验证码状态恢复到当前服务实例。"""
        if used_codes:
            self._get_used_verification_codes(email).update(
                str(code) for code in used_codes if code
            )
        if seen_messages:
            self._get_seen_verification_messages(email).update(
                str(marker) for marker in seen_messages if marker
            )

    def export_verification_state(self, email: str) -> Dict[str, List[str]]:
        """导出当前邮箱的验证码状态，用于跨请求复用。"""
        return {
            "used_codes": sorted(self._get_used_verification_codes(email)),
            "seen_messages": sorted(self._get_seen_verification_messages(email)),
        }

    def _remember_verification_code(self, email: str, code: str) -> bool:
        """记录验证码；若已用过则返回 False。"""
        used_codes = self._get_used_verification_codes(email)
        if code in used_codes:
            return False
        used_codes.add(code)
        return True

    def _remember_verification_message(self, email: str, message_marker: Optional[str]) -> bool:
        """记录消息标识；若已处理过则返回 False。"""
        if not message_marker:
            return True

        seen_messages = self._get_seen_verification_messages(email)
        if message_marker in seen_messages:
            return False
        seen_messages.add(message_marker)
        return True

    def _accept_verification_code(
        self,
        email: str,
        code: str,
        message_marker: Optional[str] = None,
    ) -> bool:
        """
        决定是否接受验证码。

        若有可靠的新邮件标识，优先按消息去重，这样新邮件即便验证码重复也能被接受；
        否则退回到按验证码去重，避免旧码被重复消费。
        """
        if message_marker:
            if not self._remember_verification_message(email, message_marker):
                return False
            self._get_used_verification_codes(email).add(code)
            return True

        return self._remember_verification_code(email, code)

    def _parse_message_timestamp(self, value: Any) -> Optional[float]:
        """将常见邮件时间字段解析为 Unix 时间戳。"""
        if value is None or value == "":
            return None

        if isinstance(value, datetime):
            return value.timestamp()

        if isinstance(value, (int, float)):
            return self._normalize_unix_timestamp(float(value))

        text = str(value).strip()
        if not text:
            return None

        try:
            return self._normalize_unix_timestamp(float(text))
        except ValueError:
            pass

        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return None

    def _normalize_unix_timestamp(self, value: float) -> float:
        """将秒/毫秒/微秒级 Unix 时间统一归一到秒。"""
        absolute = abs(value)
        if absolute >= 1e14:
            return value / 1_000_000
        if absolute >= 1e11:
            return value / 1_000
        return value

    def _is_message_before_otp(self, message_time: Any, otp_sent_at: Optional[float], tolerance_seconds: int = 1) -> bool:
        """
        判断邮件是否早于当前 OTP 发送窗口。

        允许少量时钟误差，避免接口时间与本地时间有轻微偏移时误伤新邮件。
        """
        if not otp_sent_at:
            return False

        message_ts = self._parse_message_timestamp(message_time)
        if message_ts is None:
            return False

        return message_ts + tolerance_seconds < otp_sent_at

    def _sort_items_by_message_time(self, items: List[Any], value_getter) -> List[Any]:
        """按邮件时间倒序排列，优先处理最新邮件。"""
        return sorted(
            items,
            key=lambda item: self._parse_message_timestamp(value_getter(item)) or float("-inf"),
            reverse=True,
        )

    def wait_for_email(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        check_interval: int = 3,
        expected_sender: str = None,
        expected_subject: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        等待并获取邮件（可选实现）

        Args:
            email: 邮箱地址
            email_id: 邮箱服务中的 ID
            timeout: 超时时间（秒）
            check_interval: 检查间隔（秒）
            expected_sender: 期望的发件人（包含检查）
            expected_subject: 期望的主题（包含检查）

        Returns:
            邮件信息字典，如果超时返回 None
        """
        import time

        start_time = time.time()
        last_email_id = None

        while time.time() - start_time < timeout:
            try:
                emails = self.list_emails()
                for email_info in emails:
                    email_data = email_info.get("email", {})
                    current_email_id = email_info.get("id")

                    # 检查是否是新的邮件
                    if last_email_id and current_email_id == last_email_id:
                        continue

                    # 检查邮箱地址
                    if email_data.get("address") != email:
                        continue

                    # 获取邮件列表
                    messages = self.get_email_messages(email_id or current_email_id)
                    for message in messages:
                        # 检查发件人
                        if expected_sender and expected_sender not in message.get("from", ""):
                            continue

                        # 检查主题
                        if expected_subject and expected_subject not in message.get("subject", ""):
                            continue

                        # 返回邮件信息
                        return {
                            "id": message.get("id"),
                            "from": message.get("from"),
                            "subject": message.get("subject"),
                            "content": message.get("content"),
                            "received_at": message.get("received_at"),
                            "email_info": email_info
                        }

                    # 更新最后检查的邮件 ID
                    if messages:
                        last_email_id = current_email_id

            except Exception as e:
                logger.warning(f"等待邮件时出错: {e}")

            time.sleep(check_interval)

        return None

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        """
        获取邮箱中的邮件列表（可选实现）

        Args:
            email_id: 邮箱服务中的 ID
            **kwargs: 其他参数

        Returns:
            邮件列表

        Note:
            这是可选方法，某些服务可能不支持
        """
        raise NotImplementedError("此邮箱服务不支持获取邮件列表")

    def get_message_content(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """
        获取邮件内容（可选实现）

        Args:
            email_id: 邮箱服务中的 ID
            message_id: 邮件 ID

        Returns:
            邮件内容字典

        Note:
            这是可选方法，某些服务可能不支持
        """
        raise NotImplementedError("此邮箱服务不支持获取邮件内容")

    def update_status(self, success: bool, error: Exception = None):
        """
        更新服务状态

        Args:
            success: 操作是否成功
            error: 错误信息
        """
        if success:
            self._status = EmailServiceStatus.HEALTHY
            self._last_error = None
            self._provider_backoff = reset_adaptive_backoff()
        else:
            if isinstance(error, RateLimitedEmailServiceError) or is_otp_timeout_error(error):
                self._status = EmailServiceStatus.UNAVAILABLE
                self._provider_backoff = apply_adaptive_backoff(
                    self._provider_backoff,
                    error,
                )
            else:
                self._status = EmailServiceStatus.DEGRADED
            if error:
                self._last_error = str(error)

    def __str__(self) -> str:
        """字符串表示"""
        return f"{self.name} ({self.service_type.value})"


class EmailServiceFactory:
    """邮箱服务工厂"""

    _registry: Dict[EmailServiceType, type] = {}

    @classmethod
    def register(cls, service_type: EmailServiceType, service_class: type):
        """
        注册邮箱服务类

        Args:
            service_type: 服务类型
            service_class: 服务类
        """
        if not issubclass(service_class, BaseEmailService):
            raise TypeError(f"{service_class} 必须是 BaseEmailService 的子类")
        cls._registry[service_type] = service_class
        logger.info(f"注册邮箱服务: {service_type.value} -> {service_class.__name__}")

    @classmethod
    def create(
        cls,
        service_type: EmailServiceType,
        config: Dict[str, Any],
        name: str = None
    ) -> BaseEmailService:
        """
        创建邮箱服务实例

        Args:
            service_type: 服务类型
            config: 服务配置
            name: 服务名称

        Returns:
            邮箱服务实例

        Raises:
            ValueError: 服务类型未注册或配置无效
        """
        if service_type not in cls._registry:
            raise ValueError(f"未注册的服务类型: {service_type.value}")

        service_class = cls._registry[service_type]
        try:
            instance = service_class(config, name)
            return instance
        except Exception as e:
            raise ValueError(f"创建邮箱服务失败: {e}")

    @classmethod
    def get_available_services(cls) -> List[EmailServiceType]:
        """
        获取所有已注册的服务类型

        Returns:
            已注册的服务类型列表
        """
        return list(cls._registry.keys())

    @classmethod
    def get_service_class(cls, service_type: EmailServiceType) -> Optional[type]:
        """
        获取服务类

        Args:
            service_type: 服务类型

        Returns:
            服务类，如果未注册返回 None
        """
        return cls._registry.get(service_type)


# 简化的工厂函数
def create_email_service(
    service_type: EmailServiceType,
    config: Dict[str, Any],
    name: str = None
) -> BaseEmailService:
    """
    创建邮箱服务（简化工厂函数）

    Args:
        service_type: 服务类型
        config: 服务配置
        name: 服务名称

    Returns:
        邮箱服务实例
    """
    return EmailServiceFactory.create(service_type, config, name)
