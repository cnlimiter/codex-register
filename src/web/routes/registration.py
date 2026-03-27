"""
注册任务 API 路由
"""

import asyncio
import logging
import threading
import uuid
import random
import re
import time
from datetime import datetime
from typing import List, Optional, Dict, Tuple, Any

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field

from ...database import crud
from ...database.session import get_db
from ...database.models import RegistrationTask, Proxy
from ...core.register import (
    ERROR_OTP_TIMEOUT_SECONDARY,
    RegistrationEngine,
    RegistrationResult,
)
from ...services import EmailServiceFactory, EmailServiceType
from ...services.base import BaseEmailService, EmailProviderBackoffState, OTPTimeoutEmailServiceError
from ...config.settings import get_settings
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# 任务存储（简单的内存存储，生产环境应使用 Redis）
running_tasks: dict = {}
email_service_circuit_breakers: Dict[int, EmailProviderBackoffState] = {}
_email_service_backoff_lock = threading.Lock()


# ============== Proxy Helper Functions ==============

RETRYABLE_PROXY_ERROR_PATTERN = re.compile(
    r"(?:curl(?:[^0-9]{0,8})?(35|56)\b|curl:\s*\((35|56)\))",
    re.IGNORECASE,
)


def get_proxy_for_registration(
    db,
    exclude_proxy_ids: Optional[List[int]] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """
    获取用于注册的代理

    策略：
    1. 优先从代理列表中随机选择一个启用的代理
    2. 如果代理列表为空且启用了动态代理，调用动态代理 API 获取
    3. 否则使用系统设置中的静态默认代理

    Returns:
        Tuple[proxy_url, proxy_id]: 代理 URL 和代理 ID（如果来自代理列表）
    """
    # 先尝试从代理列表中获取
    proxy = crud.get_random_proxy(db, exclude_ids=exclude_proxy_ids)
    if proxy:
        return proxy.proxy_url, proxy.id

    # 代理列表为空，尝试动态代理或静态代理
    from ...core.dynamic_proxy import get_proxy_url_for_task
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url, None

    return None, None


def update_proxy_usage(db, proxy_id: Optional[int]):
    """更新代理的使用时间"""
    if proxy_id:
        crud.update_proxy_last_used(db, proxy_id)


def is_retryable_proxy_error(error_message: Optional[str]) -> bool:
    """判断是否属于可通过切换代理自愈的 curl 网络错误。"""
    message = str(error_message or "").strip()
    if not message:
        return False
    return RETRYABLE_PROXY_ERROR_PATTERN.search(message) is not None


def disable_proxy_for_network_error(db, proxy_id: Optional[int], reason: str) -> bool:
    """将当前数据库代理标记为失效，避免后续再次被选中。"""
    if not proxy_id:
        return False

    proxy = crud.update_proxy(db, proxy_id, enabled=False)
    if not proxy:
        return False

    logger.warning(f"代理 {proxy_id} 因网络错误已自动禁用: {reason}")
    return True


# ============== Pydantic Models ==============

class RegistrationTaskCreate(BaseModel):
    """创建注册任务请求"""
    email_service_type: str = "tempmail"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []  # 指定 CPA 服务 ID 列表，空则取第一个启用的
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []  # 指定 Sub2API 服务 ID 列表
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []  # 指定 TM 服务 ID 列表


class BatchRegistrationRequest(BaseModel):
    """批量注册请求"""
    count: int = 1
    email_service_type: str = "tempmail"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []


class MockRegistrationCreateRequest(BaseModel):
    """创建受控模拟任务请求"""
    email_service_type: str = "tempmail"
    start_delay_ms: int = Field(default=300, ge=0, le=5000)
    log_delay_ms: int = Field(default=250, ge=0, le=5000)


class RegistrationTaskResponse(BaseModel):
    """注册任务响应"""
    id: int
    task_uuid: str
    status: str
    email_service_id: Optional[int] = None
    proxy: Optional[str] = None
    logs: Optional[str] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    class Config:
        from_attributes = True


class BatchRegistrationResponse(BaseModel):
    """批量注册响应"""
    batch_id: str
    count: int
    tasks: List[RegistrationTaskResponse]


class MockRegistrationTaskCreateResponse(BaseModel):
    """受控模拟任务响应"""
    task: RegistrationTaskResponse
    batch_id: str
    checks: Dict[str, Any]


class TaskListResponse(BaseModel):
    """任务列表响应"""
    total: int
    tasks: List[RegistrationTaskResponse]


# ============== Outlook 批量注册模型 ==============

class OutlookAccountForRegistration(BaseModel):
    """可用于注册的 Outlook 账户"""
    id: int                      # EmailService 表的 ID
    email: str
    name: str
    has_oauth: bool              # 是否有 OAuth 配置
    is_registered: bool          # 是否已注册
    registered_account_id: Optional[int] = None


class OutlookAccountsListResponse(BaseModel):
    """Outlook 账户列表响应"""
    total: int
    registered_count: int        # 已注册数量
    unregistered_count: int      # 未注册数量
    accounts: List[OutlookAccountForRegistration]


class OutlookBatchRegistrationRequest(BaseModel):
    """Outlook 批量注册请求"""
    service_ids: List[int]
    skip_registered: bool = True
    proxy: Optional[str] = None
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []


class OutlookBatchRegistrationResponse(BaseModel):
    """Outlook 批量注册响应"""
    batch_id: str
    total: int                   # 总数
    skipped: int                 # 跳过数（已注册）
    to_register: int             # 待注册数
    service_ids: List[int]       # 实际要注册的服务 ID


# ============== Helper Functions ==============

def task_to_response(task: RegistrationTask) -> RegistrationTaskResponse:
    """转换任务模型为响应"""
    return RegistrationTaskResponse(
        id=task.id,
        task_uuid=task.task_uuid,
        status=task.status,
        email_service_id=task.email_service_id,
        proxy=task.proxy,
        logs=task.logs,
        result=task.result,
        error_message=task.error_message,
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


def _create_task_status_callback(task_uuid: str, email_service: str):
    """把引擎内部阶段进度映射到 TaskManager 状态广播。"""

    def callback(payload: Dict[str, Any]) -> None:
        status_payload = {
            "email_service": email_service,
            **payload,
        }
        task_manager.update_status(task_uuid, "running", **status_payload)

    return callback


def _normalize_email_service_config(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None
) -> dict:
    """按服务类型兼容旧字段名，避免不同服务的配置键互相污染。"""
    normalized = config.copy() if config else {}

    if 'api_url' in normalized and 'base_url' not in normalized:
        normalized['base_url'] = normalized.pop('api_url')

    if service_type == EmailServiceType.MOE_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if 'default_domain' in normalized and 'domain' not in normalized:
            normalized['domain'] = normalized.pop('default_domain')
    elif service_type == EmailServiceType.DUCK_MAIL:
        if 'domain' in normalized and 'default_domain' not in normalized:
            normalized['default_domain'] = normalized.pop('domain')

    if proxy_url and 'proxy_url' not in normalized:
        normalized['proxy_url'] = proxy_url

    return normalized


def _get_email_service_backoff_state(service_id: Optional[int]) -> EmailProviderBackoffState:
    if service_id is None:
        return EmailProviderBackoffState()
    return email_service_circuit_breakers.get(service_id, EmailProviderBackoffState())


def _store_email_service_backoff_state(
    service_id: Optional[int],
    backoff_state: Optional[EmailProviderBackoffState],
) -> Optional[EmailProviderBackoffState]:
    if service_id is None or backoff_state is None:
        return None
    if backoff_state.failures == 0 and backoff_state.delay_seconds == 0:
        email_service_circuit_breakers.pop(service_id, None)
        return backoff_state
    email_service_circuit_breakers[service_id] = backoff_state
    return backoff_state


def _get_phase_result(phase_history, phase_name: str):
    for phase_result in phase_history or []:
        if getattr(phase_result, "phase", None) == phase_name:
            return phase_result
    return None


def _is_email_service_circuit_open(service_id: Optional[int], now: Optional[float] = None) -> bool:
    if service_id is None:
        return False
    return _get_email_service_backoff_state(service_id).is_open(now)


def _trip_email_service_circuit(
    service_id: Optional[int],
    backoff_state: Optional[EmailProviderBackoffState],
) -> int:
    if service_id is None or backoff_state is None:
        return 0
    _store_email_service_backoff_state(service_id, backoff_state)
    return backoff_state.delay_seconds


def _record_email_service_timeout_backoff(
    service_id: Optional[int],
    email_service,
    previous_backoff_state: EmailProviderBackoffState,
    error_code: str,
    error_message: str,
) -> Optional[EmailProviderBackoffState]:
    if service_id is None:
        return None

    timeout_error = OTPTimeoutEmailServiceError(
        error_message or "等待验证码超时",
        error_code=error_code,
    )
    if hasattr(email_service, "apply_provider_backoff_state"):
        email_service.apply_provider_backoff_state(previous_backoff_state)
    if hasattr(email_service, "update_status"):
        email_service.update_status(False, timeout_error)
    backoff_state = getattr(email_service, "provider_backoff_state", None)
    return _store_email_service_backoff_state(service_id, backoff_state)


def _run_registration_engine_attempt(
    task_uuid: str,
    email_service,
    actual_proxy_url: Optional[str],
    log_callback,
    db_service,
    status_callback=None,
):
    """执行单次注册引擎尝试，并在同一临界区内维护邮箱服务退避状态。"""
    provider_backoff_before_run = EmailProviderBackoffState()

    with _email_service_backoff_lock:
        if db_service is not None:
            provider_backoff_before_run = _get_email_service_backoff_state(db_service.id)
            if hasattr(email_service, "apply_provider_backoff_state"):
                email_service.apply_provider_backoff_state(provider_backoff_before_run)

        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=actual_proxy_url,
            callback_logger=log_callback,
            status_callback=status_callback,
            task_uuid=task_uuid,
        )

        try:
            result = engine.run()
        finally:
            close_engine = getattr(engine, "close", None)
            if callable(close_engine):
                close_engine()

        email_prepare_phase = _get_phase_result(
            getattr(engine, "phase_history", []),
            "email_prepare",
        )
        if db_service is not None and email_prepare_phase is not None:
            _store_email_service_backoff_state(
                db_service.id,
                getattr(email_prepare_phase, "provider_backoff", None),
            )

        if (
            db_service is not None
            and not result.success
            and result.error_code == ERROR_OTP_TIMEOUT_SECONDARY
        ):
            timeout_backoff = _record_email_service_timeout_backoff(
                db_service.id,
                email_service,
                provider_backoff_before_run,
                result.error_code,
                result.error_message,
            )
        else:
            timeout_backoff = None

    return engine, result, email_prepare_phase, provider_backoff_before_run, timeout_backoff


def _get_batch_snapshot(batch_id: str) -> Optional[dict]:
    return task_manager.get_batch_status(batch_id)


def _require_batch_snapshot(batch_id: str) -> dict:
    batch = _get_batch_snapshot(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    return batch


def _build_email_service_candidates(
    db,
    service_type: EmailServiceType,
    actual_proxy_url: Optional[str],
    email_service_id: Optional[int],
    email_service_config: Optional[dict],
) -> List[Dict[str, object]]:
    from ...database.models import EmailService as EmailServiceModel, Account

    settings = get_settings()
    candidates: List[Dict[str, object]] = []

    def append_candidate(candidate_type: EmailServiceType, config: dict, db_service=None) -> None:
        candidates.append({
            "service_type": candidate_type,
            "config": config,
            "db_service": db_service,
        })

    def append_database_candidates(db_service_type: str) -> None:
        services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == db_service_type,
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).all()

        for db_service in services:
            if _is_email_service_circuit_open(db_service.id):
                continue
            candidate_type = EmailServiceType(db_service.service_type)
            config = _normalize_email_service_config(candidate_type, db_service.config, actual_proxy_url)
            append_candidate(candidate_type, config, db_service=db_service)

    if email_service_id:
        db_service = db.query(EmailServiceModel).filter(
            EmailServiceModel.id == email_service_id,
            EmailServiceModel.enabled == True
        ).first()
        if not db_service:
            raise ValueError(f"邮箱服务不存在或已禁用: {email_service_id}")
        if _is_email_service_circuit_open(db_service.id):
            raise ValueError(f"邮箱服务处于熔断状态: {db_service.name}")
        candidate_type = EmailServiceType(db_service.service_type)
        config = _normalize_email_service_config(candidate_type, db_service.config, actual_proxy_url)
        append_candidate(candidate_type, config, db_service=db_service)
        return candidates

    if service_type == EmailServiceType.TEMPMAIL:
        append_candidate(service_type, {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": actual_proxy_url,
        })
    elif service_type == EmailServiceType.MOE_MAIL:
        append_database_candidates("moe_mail")
        if not candidates:
            if settings.custom_domain_base_url and settings.custom_domain_api_key:
                append_candidate(service_type, {
                    "base_url": settings.custom_domain_base_url,
                    "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                    "proxy_url": actual_proxy_url,
                })
            else:
                raise ValueError("没有可用的自定义域名邮箱服务，请先在设置中配置")
    elif service_type == EmailServiceType.OUTLOOK:
        services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).all()

        if not services:
            raise ValueError("没有可用的 Outlook 账户，请先在设置中导入账户")

        for db_service in services:
            if _is_email_service_circuit_open(db_service.id):
                continue
            email = db_service.config.get("email") if db_service.config else None
            if not email:
                continue
            existing = db.query(Account).filter(Account.email == email).first()
            if existing:
                logger.info(f"跳过已注册的 Outlook 账户: {email}")
                continue
            config = _normalize_email_service_config(service_type, db_service.config, actual_proxy_url)
            append_candidate(service_type, config, db_service=db_service)

        if not candidates:
            raise ValueError("所有 Outlook 账户都已注册过，或当前均处于熔断状态")
    elif service_type == EmailServiceType.DUCK_MAIL:
        append_database_candidates("duck_mail")
        if not candidates:
            raise ValueError("没有可用的 DuckMail 邮箱服务，请先在邮箱服务页面添加服务")
    elif service_type == EmailServiceType.FREEMAIL:
        append_database_candidates("freemail")
        if not candidates:
            raise ValueError("没有可用的 Freemail 邮箱服务，请先在邮箱服务页面添加服务")
    elif service_type == EmailServiceType.IMAP_MAIL:
        append_database_candidates("imap_mail")
        if not candidates:
            raise ValueError("没有可用的 IMAP 邮箱服务，请先在邮箱服务中添加")
    else:
        append_candidate(service_type, email_service_config or {})

    return candidates


def _run_sync_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None):
    """
    在线程池中执行的同步注册任务

    这个函数会被 run_in_executor 调用，运行在独立线程中
    """
    with get_db() as db:
        try:
            if task_manager.is_cancelled(task_uuid):
                logger.info(f"任务 {task_uuid} 已取消，跳过执行")
                return

            task = crud.update_registration_task(
                db, task_uuid,
                status="running",
                started_at=datetime.utcnow()
            )
            if not task:
                logger.error(f"任务不存在: {task_uuid}")
                return

            task_manager.update_status(task_uuid, "running")
            log_callback = task_manager.create_log_callback(task_uuid, prefix=log_prefix, batch_id=batch_id)
            requested_service_type = EmailServiceType(email_service_type)
            requested_proxy = proxy
            exhausted_proxy_ids = set()
            result = RegistrationResult(success=False, logs=[])
            active_service_type = requested_service_type
            proxy_id = None

            while True:
                actual_proxy_url = requested_proxy
                proxy_id = None
                gateway_switched = False
                try:
                    if not actual_proxy_url:
                        actual_proxy_url, proxy_id = get_proxy_for_registration(
                            db,
                            exclude_proxy_ids=list(exhausted_proxy_ids),
                        )
                        if actual_proxy_url:
                            logger.info(f"任务 {task_uuid} 使用代理: {actual_proxy_url[:50]}...")

                    crud.update_registration_task(db, task_uuid, proxy=actual_proxy_url)
                    service_candidates = _build_email_service_candidates(
                        db,
                        requested_service_type,
                        actual_proxy_url,
                        email_service_id,
                        email_service_config,
                    )

                    should_retry_with_new_proxy = False

                    # 使用任意代理时，临时将默认网关切到代理出口网关；结束后恢复。
                    if actual_proxy_url:
                        try:
                            import subprocess
                            out = subprocess.run(
                                ["bash", "/app/scripts/switch_gateway.sh", "proxy"],
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            msg = (out.stdout or out.stderr).strip()
                            if out.returncode == 0:
                                log_callback(f"[网关] 切换到代理网关: {msg}")
                                gateway_switched = True
                            else:
                                log_callback(f"[网关] 切换代理网关失败(returncode={out.returncode}): {msg}")
                        except Exception as e:
                            log_callback(f"[网关] 切换代理网关失败: {e}")

                    for attempt_index, candidate in enumerate(service_candidates, start=1):
                        selected_service_type = candidate["service_type"]
                        candidate_config = candidate["config"]
                        db_service = candidate.get("db_service")
                        active_service_type = selected_service_type

                        if db_service is not None:
                            crud.update_registration_task(db, task_uuid, email_service_id=db_service.id)
                            logger.info(
                                f"任务 {task_uuid} 使用数据库邮箱服务: {db_service.name} "
                                f"(ID: {db_service.id}, 类型: {selected_service_type.value}, 尝试: {attempt_index}/{len(service_candidates)})"
                            )
                            log_callback(
                                f"[系统] 使用邮箱服务: {db_service.name} "
                                f"({selected_service_type.value}, 尝试 {attempt_index}/{len(service_candidates)})"
                            )
                        else:
                            crud.update_registration_task(db, task_uuid, email_service_id=None)

                        task_manager.update_status(task_uuid, "running", email_service=active_service_type.value)
                        status_callback = _create_task_status_callback(task_uuid, active_service_type.value)
                        email_service = EmailServiceFactory.create(
                            selected_service_type,
                            candidate_config,
                            name=db_service.name if db_service is not None else None,
                        )
                        (
                            engine,
                            result,
                            email_prepare_phase,
                            _,
                            timeout_backoff,
                        ) = _run_registration_engine_attempt(
                            task_uuid=task_uuid,
                            email_service=email_service,
                            actual_proxy_url=actual_proxy_url,
                            log_callback=log_callback,
                            db_service=db_service,
                            status_callback=status_callback,
                        )

                        if result.success:
                            break

                        if is_retryable_proxy_error(result.error_message):
                            should_retry_with_new_proxy = True
                            break

                        can_failover = (
                            db_service is not None
                            and attempt_index < len(service_candidates)
                            and email_prepare_phase is not None
                            and not email_prepare_phase.success
                            and email_prepare_phase.error_code == "EMAIL_PROVIDER_RATE_LIMITED"
                            and email_prepare_phase.provider_backoff is not None
                        )
                        if not can_failover:
                            if timeout_backoff is not None:
                                logger.warning(
                                    f"邮箱服务 OTP 超时，已退避 {db_service.name} "
                                    f"{timeout_backoff.delay_seconds} 秒，连续失败 "
                                    f"{timeout_backoff.failures} 次"
                                )
                                log_callback(
                                    f"[系统] 邮箱服务 OTP 超时，退避 "
                                    f"{timeout_backoff.delay_seconds} 秒: {db_service.name} "
                                    f"(连续失败 {timeout_backoff.failures} 次)"
                                )
                            break

                        backoff_state = email_prepare_phase.provider_backoff
                        cooldown = _trip_email_service_circuit(db_service.id, backoff_state)
                        logger.warning(
                            f"邮箱服务限流，已退避 {db_service.name} {cooldown} 秒，"
                            f"连续失败 {backoff_state.failures} 次，"
                            f"任务 {task_uuid} 将切换到下一个服务"
                        )
                        log_callback(
                            f"[系统] 邮箱服务限流，退避 {cooldown} 秒并切换: "
                            f"{db_service.name} (连续失败 {backoff_state.failures} 次)"
                        )
                finally:
                    if gateway_switched:
                        try:
                            import subprocess
                            out = subprocess.run(
                                ["bash", "/app/scripts/switch_gateway.sh", "default"],
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            msg = (out.stdout or out.stderr).strip()
                            if out.returncode == 0:
                                log_callback(f"[网关] 恢复默认网关: {msg}")
                            else:
                                log_callback(f"[网关] 恢复默认网关失败(returncode={out.returncode}): {msg}")
                        except Exception as e:
                            log_callback(f"[网关] 恢复默认网关失败: {e}")

                if result.success:
                    break

                if should_retry_with_new_proxy:
                    log_callback(f"[代理] 检测到可重试网络错误: {result.error_message}")
                    if proxy_id and disable_proxy_for_network_error(db, proxy_id, result.error_message):
                        exhausted_proxy_ids.add(proxy_id)
                        log_callback(f"[代理] 当前代理已标记失效并从代理池移除: {proxy_id}")

                    next_proxy_url, next_proxy_id = get_proxy_for_registration(
                        db,
                        exclude_proxy_ids=list(exhausted_proxy_ids),
                    )
                    if next_proxy_url and (next_proxy_url != actual_proxy_url or next_proxy_id != proxy_id):
                        requested_proxy = None
                        log_callback(f"[代理] 切换到新代理后重试注册: {next_proxy_url[:50]}...")
                        continue

                break

            if result.success:
                # 更新代理使用时间
                update_proxy_usage(db, proxy_id)

                # 保存到数据库
                engine.save_to_database(result)

                # 自动上传到 CPA（可多服务）
                if auto_upload_cpa:
                    try:
                        from ...core.upload.cpa_upload import upload_to_cpa, generate_token_json
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _cpa_ids = cpa_service_ids or []
                            if not _cpa_ids:
                                # 未指定则取所有启用的服务
                                _cpa_ids = [s.id for s in crud.get_cpa_services(db, enabled=True)]
                            if not _cpa_ids:
                                log_callback("[CPA] 无可用 CPA 服务，跳过上传")
                            for _sid in _cpa_ids:
                                try:
                                    _svc = crud.get_cpa_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    token_data = generate_token_json(
                                        saved_account,
                                        include_proxy_url=bool(_svc.include_proxy_url),
                                    )
                                    log_callback(f"[CPA] 上传到服务: {_svc.name}")
                                    _ok, _msg = upload_to_cpa(token_data, api_url=_svc.api_url, api_token=_svc.api_token)
                                    if _ok:
                                        saved_account.cpa_uploaded = True
                                        saved_account.cpa_uploaded_at = datetime.utcnow()
                                        db.commit()
                                        log_callback(f"[CPA] 上传成功: {_svc.name}")
                                    else:
                                        log_callback(f"[CPA] 上传失败({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[CPA] 异常({_sid}): {_e}")
                    except Exception as cpa_err:
                        log_callback(f"[CPA] 上传异常: {cpa_err}")

                # 自动上传到 Sub2API（可多服务）
                if auto_upload_sub2api:
                    try:
                        from ...core.upload.sub2api_upload import upload_to_sub2api
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _s2a_ids = sub2api_service_ids or []
                            if not _s2a_ids:
                                _s2a_ids = [s.id for s in crud.get_sub2api_services(db, enabled=True)]
                            if not _s2a_ids:
                                log_callback("[Sub2API] 无可用 Sub2API 服务，跳过上传")
                            for _sid in _s2a_ids:
                                try:
                                    _svc = crud.get_sub2api_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[Sub2API] 上传到服务: {_svc.name}")
                                    _ok, _msg = upload_to_sub2api([saved_account], _svc.api_url, _svc.api_key)
                                    log_callback(f"[Sub2API] {'成功' if _ok else '失败'}({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[Sub2API] 异常({_sid}): {_e}")
                    except Exception as s2a_err:
                        log_callback(f"[Sub2API] 上传异常: {s2a_err}")

                # 自动上传到 Team Manager（可多服务）
                if auto_upload_tm:
                    try:
                        from ...core.upload.team_manager_upload import upload_to_team_manager
                        from ...database.models import Account as AccountModel
                        saved_account = db.query(AccountModel).filter_by(email=result.email).first()
                        if saved_account and saved_account.access_token:
                            _tm_ids = tm_service_ids or []
                            if not _tm_ids:
                                _tm_ids = [s.id for s in crud.get_tm_services(db, enabled=True)]
                            if not _tm_ids:
                                log_callback("[TM] 无可用 Team Manager 服务，跳过上传")
                            for _sid in _tm_ids:
                                try:
                                    _svc = crud.get_tm_service_by_id(db, _sid)
                                    if not _svc:
                                        continue
                                    log_callback(f"[TM] 上传到服务: {_svc.name}")
                                    _ok, _msg = upload_to_team_manager(saved_account, _svc.api_url, _svc.api_key)
                                    log_callback(f"[TM] {'成功' if _ok else '失败'}({_svc.name}): {_msg}")
                                except Exception as _e:
                                    log_callback(f"[TM] 异常({_sid}): {_e}")
                    except Exception as tm_err:
                        log_callback(f"[TM] 上传异常: {tm_err}")

                # 更新任务状态
                crud.update_registration_task(
                    db, task_uuid,
                    status="completed",
                    completed_at=datetime.utcnow(),
                    result={
                        **result.to_dict(),
                        "email_service": active_service_type.value,
                    }
                )

                # 更新 TaskManager 状态
                task_manager.update_status(
                    task_uuid,
                    "completed",
                    email=result.email,
                    email_service=active_service_type.value,
                )

                logger.info(f"注册任务完成: {task_uuid}, 邮箱: {result.email}")
            else:
                # 更新任务状态为失败
                crud.update_registration_task(
                    db, task_uuid,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=result.error_message
                )

                # 更新 TaskManager 状态
                task_manager.update_status(
                    task_uuid,
                    "failed",
                    error=result.error_message,
                    email_service=active_service_type.value,
                )

                logger.warning(f"注册任务失败: {task_uuid}, 原因: {result.error_message}")

        except Exception as e:
            logger.error(f"注册任务异常: {task_uuid}, 错误: {e}")

            try:
                with get_db() as db:
                    crud.update_registration_task(
                        db, task_uuid,
                        status="failed",
                        completed_at=datetime.utcnow(),
                        error_message=str(e)
                    )

                # 更新 TaskManager 状态
                task_manager.update_status(task_uuid, "failed", error=str(e))
            except:
                pass


async def run_registration_task(task_uuid: str, email_service_type: str, proxy: Optional[str], email_service_config: Optional[dict], email_service_id: Optional[int] = None, log_prefix: str = "", batch_id: str = "", auto_upload_cpa: bool = False, cpa_service_ids: List[int] = None, auto_upload_sub2api: bool = False, sub2api_service_ids: List[int] = None, auto_upload_tm: bool = False, tm_service_ids: List[int] = None):
    """
    异步执行注册任务

    使用 run_in_executor 将同步任务放入线程池执行，避免阻塞主事件循环
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 初始化 TaskManager 状态
    task_manager.update_status(task_uuid, "pending", email_service=email_service_type)
    task_manager.add_log(task_uuid, f"{log_prefix} [系统] 任务 {task_uuid[:8]} 已加入队列" if log_prefix else f"[系统] 任务 {task_uuid[:8]} 已加入队列")

    try:
        # 在线程池中执行同步任务（传入 log_prefix 和 batch_id 供回调使用）
        await loop.run_in_executor(
            task_manager.executor,
            _run_sync_registration_task,
            task_uuid,
            email_service_type,
            proxy,
            email_service_config,
            email_service_id,
            log_prefix,
            batch_id,
            auto_upload_cpa,
            cpa_service_ids or [],
            auto_upload_sub2api,
            sub2api_service_ids or [],
            auto_upload_tm,
            tm_service_ids or [],
        )
    except Exception as e:
        logger.error(f"线程池执行异常: {task_uuid}, 错误: {e}")
        task_manager.add_log(task_uuid, f"[错误] 线程池执行异常: {str(e)}")
        task_manager.update_status(task_uuid, "failed", error=str(e))


def _init_batch_state(batch_id: str, task_uuids: List[str]):
    """初始化批量任务内存状态"""
    task_manager.init_batch(batch_id, len(task_uuids), task_uuids=task_uuids)


def _make_batch_helpers(batch_id: str):
    """返回 add_batch_log 和 update_batch_status 辅助函数"""
    def add_batch_log(msg: str):
        task_manager.add_batch_log(batch_id, msg)

    def update_batch_status(**kwargs):
        task_manager.update_batch_status(batch_id, **kwargs)

    return add_batch_log, update_batch_status


class _MockBackoffEmailService(BaseEmailService):
    """用于真实服务验证的最小邮箱服务桩。"""

    def __init__(self):
        super().__init__(service_type=EmailServiceType.DUCK_MAIL, name="mock-backoff-service")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        return {"email": "mock@example.test", "service_id": "mock-service-id"}

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def _create_persisted_log_callback(task_uuid: str, prefix: str = "", batch_id: str = ""):
    """同时写入内存日志队列、批量日志通道和数据库任务日志。"""

    def callback(message: str) -> None:
        full_message = f"{prefix} {message}" if prefix else message
        task_manager.add_log(task_uuid, full_message)
        if batch_id:
            task_manager.add_batch_log(batch_id, full_message)
        with get_db() as db:
            crud.append_task_log(db, task_uuid, full_message)

    return callback


def _simulate_batch_counter_probe(batch_id: str) -> Dict[str, Any]:
    """构造一个可重复的批量计数场景，验证 TaskManager 计数收口。"""
    task_uuids = [str(uuid.uuid4()) for _ in range(3)]
    task_statuses = ["completed", "failed", "completed"]
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    add_batch_log(f"[系统] 模拟批量任务启动，总任务: {len(task_uuids)}")

    with get_db() as db:
        for index, (task_uuid, status) in enumerate(zip(task_uuids, task_statuses), start=1):
            crud.create_registration_task(db, task_uuid=task_uuid, proxy=None)
            error_message = None if status == "completed" else f"mock-batch-error-{index}"
            crud.update_registration_task(
                db,
                task_uuid,
                status=status,
                started_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
                error_message=error_message,
            )

            batch_snapshot = _get_batch_snapshot(batch_id) or {}
            new_completed = batch_snapshot.get("completed", 0) + 1
            new_success = batch_snapshot.get("success", 0)
            new_failed = batch_snapshot.get("failed", 0)
            if status == "completed":
                new_success += 1
                add_batch_log(f"[任务{index}] [成功] 模拟注册成功")
            else:
                new_failed += 1
                add_batch_log(f"[任务{index}] [失败] 模拟注册失败: {error_message}")
            update_batch_status(completed=new_completed, success=new_success, failed=new_failed)

    batch_snapshot = _get_batch_snapshot(batch_id) or {}
    add_batch_log(
        f"[完成] 批量任务完成！成功: {batch_snapshot.get('success', 0)}, "
        f"失败: {batch_snapshot.get('failed', 0)}"
    )
    update_batch_status(finished=True, status="completed")
    return {
        "batch_id": batch_id,
        "task_uuids": task_uuids,
        "snapshot": task_manager.get_batch_status(batch_id) or {},
    }


async def run_mock_registration_task(
    task_uuid: str,
    batch_id: str,
    checks: Dict[str, Any],
    email_service_type: str,
    start_delay_ms: int,
    log_delay_ms: int,
) -> None:
    """通过真实服务链路执行可重复的模拟任务。"""
    if start_delay_ms > 0:
        await asyncio.sleep(start_delay_ms / 1000)

    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    log_callback = _create_persisted_log_callback(task_uuid)
    delay_seconds = max(log_delay_ms, 0) / 1000

    try:
        with get_db() as db:
            task = crud.update_registration_task(
                db,
                task_uuid,
                status="running",
                started_at=datetime.utcnow(),
            )
        if not task:
            logger.error(f"模拟任务不存在: {task_uuid}")
            return

        task_manager.update_status(task_uuid, "running", email_service=email_service_type)
        log_callback("[模拟] 任务已启动，开始执行真实链路探针")
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        with get_db() as db:
            seeded_account = crud.create_account(
                db,
                email=checks["seeded_account_email"],
                email_service="tempmail",
                access_token="mock-access-token-seeded",
                refresh_token="mock-refresh-token-seeded",
            )
            tokenless_account = crud.create_account(
                db,
                email=checks["tokenless_account_email"],
                email_service="tempmail",
            )
            crud.update_account(
                db,
                tokenless_account.id,
                access_token="mock-access-token-updated",
            )
            partial_account = crud.create_account(
                db,
                email=checks["partial_account_email"],
                email_service="tempmail",
                access_token="mock-access-token-partial",
                refresh_token="mock-refresh-token-partial",
            )
            crud.update_account(
                db,
                partial_account.id,
                refresh_token="",
            )
            outlook_service = crud.create_email_service(
                db,
                service_type="outlook",
                name=f"mock-outlook-{task_uuid[:8]}",
                config={
                    "accounts": [
                        {"email": "first@example.test", "refresh_token": "old-first"},
                        {
                            "email": checks["outlook_account_email"],
                            "refresh_token": "old-second",
                        },
                    ]
                },
            )
            crud.update_outlook_refresh_token(
                db,
                service_id=outlook_service.id,
                email=checks["outlook_account_email"],
                new_refresh_token="new-second",
            )
            backoff_service = crud.create_email_service(
                db,
                service_type="duck_mail",
                name=checks["backoff_service_name"],
                config={
                    "base_url": "https://mail.example.test",
                    "default_domain": "example.test",
                },
            )
            checks["seeded_account_id"] = seeded_account.id
            checks["tokenless_account_id"] = tokenless_account.id
            checks["partial_account_id"] = partial_account.id
            checks["outlook_service_id"] = outlook_service.id
            checks["backoff_service_id"] = backoff_service.id
        log_callback("[模拟] Token 同步与 Outlook refresh_token 探针已写入数据库")
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        mock_email_service = _MockBackoffEmailService()
        backoff_states = []
        for attempt in range(1, 4):
            previous_state = _get_email_service_backoff_state(backoff_service.id)
            current_state = _record_email_service_timeout_backoff(
                backoff_service.id,
                mock_email_service,
                previous_state,
                ERROR_OTP_TIMEOUT_SECONDARY,
                f"模拟 OTP 超时 #{attempt}",
            )
            if current_state is not None:
                backoff_states.append(current_state.to_dict())
                log_callback(
                    f"[模拟] OTP 超时退避 #{attempt}: "
                    f"failures={current_state.failures}, delay={current_state.delay_seconds}"
                )
            if delay_seconds:
                await asyncio.sleep(delay_seconds)

        batch_probe = _simulate_batch_counter_probe(batch_id)
        log_callback("[模拟] 批量计数探针已完成")
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        result = {
            "email": checks["seeded_account_email"],
            "email_service": email_service_type,
            "hardening_checks": {
                "token_sync": {
                    "seeded_account_id": checks["seeded_account_id"],
                    "tokenless_account_id": checks["tokenless_account_id"],
                    "partial_account_id": checks["partial_account_id"],
                },
                "outlook_refresh": {
                    "service_id": checks["outlook_service_id"],
                    "email": checks["outlook_account_email"],
                },
                "batch_counter": batch_probe,
                "otp_timeout_backoff": {
                    "service_id": checks["backoff_service_id"],
                    "states": backoff_states,
                },
            },
        }

        with get_db() as db:
            crud.update_registration_task(
                db,
                task_uuid,
                status="completed",
                completed_at=datetime.utcnow(),
                result=result,
            )
        task_manager.update_status(
            task_uuid,
            "completed",
            email=checks["seeded_account_email"],
            email_service=email_service_type,
        )
        log_callback("[模拟] 任务完成，所有探针已收口")
    except Exception as exc:
        logger.exception("模拟任务执行失败: %s", task_uuid)
        with get_db() as db:
            crud.update_registration_task(
                db,
                task_uuid,
                status="failed",
                completed_at=datetime.utcnow(),
                error_message=str(exc),
            )
        task_manager.update_status(task_uuid, "failed", error=str(exc), email_service=email_service_type)
        log_callback(f"[模拟] 任务失败: {exc}")


async def run_batch_parallel(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    并行模式：所有任务同时提交，Semaphore 控制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    add_batch_log(f"[系统] 并行模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_one(idx: int, uuid: str):
        prefix = f"[任务{idx + 1}]"
        async with semaphore:
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=prefix, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
            )
        with get_db() as db:
            t = crud.get_registration_task(db, uuid)
            if t:
                async with counter_lock:
                    batch_snapshot = _get_batch_snapshot(batch_id) or {}
                    new_completed = batch_snapshot.get("completed", 0) + 1
                    new_success = batch_snapshot.get("success", 0)
                    new_failed = batch_snapshot.get("failed", 0)
                    if t.status == "completed":
                        new_success += 1
                        add_batch_log(f"{prefix} [成功] 注册成功")
                    elif t.status == "failed":
                        new_failed += 1
                        add_batch_log(f"{prefix} [失败] 注册失败: {t.error_message}")
                    update_batch_status(completed=new_completed, success=new_success, failed=new_failed)

    try:
        await asyncio.gather(*[_run_one(i, u) for i, u in enumerate(task_uuids)], return_exceptions=True)
        if not task_manager.is_batch_cancelled(batch_id):
            batch_snapshot = _get_batch_snapshot(batch_id) or {}
            add_batch_log(
                f"[完成] 批量任务完成！成功: {batch_snapshot.get('success', 0)}, "
                f"失败: {batch_snapshot.get('failed', 0)}"
            )
            update_batch_status(finished=True, status="completed")
        else:
            update_batch_status(finished=True, status="cancelled")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")


async def run_batch_pipeline(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    interval_min: int,
    interval_max: int,
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    流水线模式：每隔 interval 秒启动一个新任务，Semaphore 限制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    running_tasks_list = []
    add_batch_log(f"[系统] 流水线模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_and_release(idx: int, uuid: str, pfx: str):
        try:
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                log_prefix=pfx, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
            )
            with get_db() as db:
                t = crud.get_registration_task(db, uuid)
                if t:
                    async with counter_lock:
                        batch_snapshot = _get_batch_snapshot(batch_id) or {}
                        new_completed = batch_snapshot.get("completed", 0) + 1
                        new_success = batch_snapshot.get("success", 0)
                        new_failed = batch_snapshot.get("failed", 0)
                        if t.status == "completed":
                            new_success += 1
                            add_batch_log(f"{pfx} [成功] 注册成功")
                        elif t.status == "failed":
                            new_failed += 1
                            add_batch_log(f"{pfx} [失败] 注册失败: {t.error_message}")
                        update_batch_status(completed=new_completed, success=new_success, failed=new_failed)
        finally:
            semaphore.release()

    try:
        for i, task_uuid in enumerate(task_uuids):
            if task_manager.is_batch_cancelled(batch_id):
                with get_db() as db:
                    for remaining_uuid in task_uuids[i:]:
                        crud.update_registration_task(db, remaining_uuid, status="cancelled")
                add_batch_log("[取消] 批量任务已取消")
                update_batch_status(finished=True, status="cancelled")
                break

            update_batch_status(current_index=i)
            await semaphore.acquire()
            prefix = f"[任务{i + 1}]"
            add_batch_log(f"{prefix} 开始注册...")
            t = asyncio.create_task(_run_and_release(i, task_uuid, prefix))
            running_tasks_list.append(t)

            if i < len(task_uuids) - 1 and not task_manager.is_batch_cancelled(batch_id):
                wait_time = random.randint(interval_min, interval_max)
                logger.info(f"批量任务 {batch_id}: 等待 {wait_time} 秒后启动下一个任务")
                await asyncio.sleep(wait_time)

        if running_tasks_list:
            await asyncio.gather(*running_tasks_list, return_exceptions=True)

        if not task_manager.is_batch_cancelled(batch_id):
            batch_snapshot = _get_batch_snapshot(batch_id) or {}
            add_batch_log(
                f"[完成] 批量任务完成！成功: {batch_snapshot.get('success', 0)}, "
                f"失败: {batch_snapshot.get('failed', 0)}"
            )
            update_batch_status(finished=True, status="completed")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")


async def run_batch_registration(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    interval_min: int,
    interval_max: int,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """根据 mode 分发到并行或流水线执行"""
    if mode == "parallel":
        await run_batch_parallel(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
        )
    else:
        await run_batch_pipeline(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id,
            interval_min, interval_max, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
        )


# ============== API Endpoints ==============

@router.post("/create", response_model=MockRegistrationTaskCreateResponse)
async def create_mock_registration(
    request: MockRegistrationCreateRequest,
    background_tasks: BackgroundTasks,
):
    """创建用于端到端验证的受控模拟任务。"""
    try:
        EmailServiceType(request.email_service_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {request.email_service_type}"
        )

    task_uuid = str(uuid.uuid4())
    suffix = task_uuid[:8]
    batch_id = str(uuid.uuid4())
    checks: Dict[str, Any] = {
        "seeded_account_email": f"mock-seeded-{suffix}@example.test",
        "tokenless_account_email": f"mock-tokenless-{suffix}@example.test",
        "partial_account_email": f"mock-partial-{suffix}@example.test",
        "outlook_account_email": f"mock-outlook-{suffix}@example.test",
        "backoff_service_name": f"mock-backoff-{suffix}",
    }

    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            proxy=None,
        )

    background_tasks.add_task(
        run_mock_registration_task,
        task_uuid,
        batch_id,
        checks,
        request.email_service_type,
        request.start_delay_ms,
        request.log_delay_ms,
    )

    return MockRegistrationTaskCreateResponse(
        task=task_to_response(task),
        batch_id=batch_id,
        checks=checks,
    )


@router.post("/start", response_model=RegistrationTaskResponse)
async def start_registration(
    request: RegistrationTaskCreate,
    background_tasks: BackgroundTasks
):
    """
    启动注册任务

    - email_service_type: 邮箱服务类型 (tempmail, outlook, moe_mail)
    - proxy: 代理地址
    - email_service_config: 邮箱服务配置（outlook 需要提供账户信息）
    """
    # 验证邮箱服务类型
    try:
        EmailServiceType(request.email_service_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {request.email_service_type}"
        )

    # 创建任务
    task_uuid = str(uuid.uuid4())

    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            proxy=request.proxy
        )

    # 在后台运行注册任务
    background_tasks.add_task(
        run_registration_task,
        task_uuid,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        "",
        "",
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    return task_to_response(task)


@router.post("/batch", response_model=BatchRegistrationResponse)
async def start_batch_registration(
    request: BatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    """
    启动批量注册任务

    - count: 注册数量 (1-100)
    - email_service_type: 邮箱服务类型
    - proxy: 代理地址
    - interval_min: 最小间隔秒数
    - interval_max: 最大间隔秒数
    """
    # 验证参数
    if request.count < 1 or request.count > 100:
        raise HTTPException(status_code=400, detail="注册数量必须在 1-100 之间")

    try:
        EmailServiceType(request.email_service_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {request.email_service_type}"
        )

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    # 创建批量任务
    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(request.count):
            task_uuid = str(uuid.uuid4())
            task = crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=request.proxy
            )
            task_uuids.append(task_uuid)

    # 获取所有任务
    with get_db() as db:
        tasks = [crud.get_registration_task(db, uuid) for uuid in task_uuids]

    # 在后台运行批量注册
    background_tasks.add_task(
        run_batch_registration,
        batch_id,
        task_uuids,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    return BatchRegistrationResponse(
        batch_id=batch_id,
        count=request.count,
        tasks=[task_to_response(t) for t in tasks if t]
    )


@router.get("/batch/{batch_id}")
async def get_batch_status(batch_id: str):
    """获取批量任务状态"""
    batch = _require_batch_snapshot(batch_id)
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """取消批量任务"""
    batch = _require_batch_snapshot(batch_id)
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    task_manager.cancel_batch(batch_id)
    return {"success": True, "message": "批量任务取消请求已提交"}


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
):
    """获取任务列表"""
    with get_db() as db:
        query = db.query(RegistrationTask)

        if status:
            query = query.filter(RegistrationTask.status == status)

        total = query.count()
        offset = (page - 1) * page_size
        tasks = query.order_by(RegistrationTask.created_at.desc()).offset(offset).limit(page_size).all()

        return TaskListResponse(
            total=total,
            tasks=[task_to_response(t) for t in tasks]
        )


@router.get("/tasks/{task_uuid}", response_model=RegistrationTaskResponse)
async def get_task(task_uuid: str):
    """获取任务详情"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task_to_response(task)


@router.get("/tasks/{task_uuid}/logs")
async def get_task_logs(task_uuid: str):
    """获取任务日志"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        logs = task.logs or ""
        return {
            "task_uuid": task_uuid,
            "status": task.status,
            "logs": logs.split("\n") if logs else []
        }


@router.post("/tasks/{task_uuid}/cancel")
async def cancel_task(task_uuid: str):
    """取消任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status not in ["pending", "running"]:
            raise HTTPException(status_code=400, detail="任务已完成或已取消")

        task = crud.update_registration_task(db, task_uuid, status="cancelled")

        return {"success": True, "message": "任务已取消"}


@router.delete("/tasks/{task_uuid}")
async def delete_task(task_uuid: str):
    """删除任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status == "running":
            raise HTTPException(status_code=400, detail="无法删除运行中的任务")

        crud.delete_registration_task(db, task_uuid)

        return {"success": True, "message": "任务已删除"}


@router.get("/stats")
async def get_registration_stats():
    """获取注册统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 按状态统计
        status_stats = db.query(
            RegistrationTask.status,
            func.count(RegistrationTask.id)
        ).group_by(RegistrationTask.status).all()

        # 今日注册数
        today = datetime.utcnow().date()
        today_count = db.query(func.count(RegistrationTask.id)).filter(
            func.date(RegistrationTask.created_at) == today
        ).scalar()

        return {
            "by_status": {status: count for status, count in status_stats},
            "today_count": today_count
        }


@router.get("/available-services")
async def get_available_email_services():
    """
    获取可用于注册的邮箱服务列表

    返回所有已启用的邮箱服务，包括：
    - tempmail: 临时邮箱（无需配置）
    - outlook: 已导入的 Outlook 账户
    - moe_mail: 已配置的自定义域名服务
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...config.settings import get_settings

    settings = get_settings()
    result = {
        "tempmail": {
            "available": True,
            "count": 1,
            "services": [{
                "id": None,
                "name": "Tempmail.lol",
                "type": "tempmail",
                "description": "临时邮箱，自动创建"
            }]
        },
        "outlook": {
            "available": False,
            "count": 0,
            "services": []
        },
        "moe_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "temp_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "duck_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "freemail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "imap_mail": {
            "available": False,
            "count": 0,
            "services": []
        }
    }

    with get_db() as db:
        # 获取 Outlook 账户
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in outlook_services:
            config = service.config or {}
            result["outlook"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "outlook",
                "has_oauth": bool(config.get("client_id") and config.get("refresh_token")),
                "priority": service.priority
            })

        result["outlook"]["count"] = len(outlook_services)
        result["outlook"]["available"] = len(outlook_services) > 0

        # 获取自定义域名服务
        custom_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "moe_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in custom_services:
            config = service.config or {}
            result["moe_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "moe_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["moe_mail"]["count"] = len(custom_services)
        result["moe_mail"]["available"] = len(custom_services) > 0

        # 如果数据库中没有自定义域名服务，检查 settings
        if not result["moe_mail"]["available"]:
            if settings.custom_domain_base_url and settings.custom_domain_api_key:
                result["moe_mail"]["available"] = True
                result["moe_mail"]["count"] = 1
                result["moe_mail"]["services"].append({
                    "id": None,
                    "name": "默认自定义域名服务",
                    "type": "moe_mail",
                    "from_settings": True
                })

        # 获取 TempMail 服务（自部署 Cloudflare Worker 临时邮箱）
        temp_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "temp_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in temp_mail_services:
            config = service.config or {}
            result["temp_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "temp_mail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["temp_mail"]["count"] = len(temp_mail_services)
        result["temp_mail"]["available"] = len(temp_mail_services) > 0

        duck_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "duck_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in duck_mail_services:
            config = service.config or {}
            result["duck_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "duck_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["duck_mail"]["count"] = len(duck_mail_services)
        result["duck_mail"]["available"] = len(duck_mail_services) > 0

        freemail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "freemail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in freemail_services:
            config = service.config or {}
            result["freemail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "freemail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["freemail"]["count"] = len(freemail_services)
        result["freemail"]["available"] = len(freemail_services) > 0

        imap_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "imap_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in imap_mail_services:
            config = service.config or {}
            result["imap_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "imap_mail",
                "email": config.get("email"),
                "host": config.get("host"),
                "priority": service.priority
            })

        result["imap_mail"]["count"] = len(imap_mail_services)
        result["imap_mail"]["available"] = len(imap_mail_services) > 0

    return result


# ============== Outlook 批量注册 API ==============

@router.get("/outlook-accounts", response_model=OutlookAccountsListResponse)
async def get_outlook_accounts_for_registration():
    """
    获取可用于注册的 Outlook 账户列表

    返回所有已启用的 Outlook 服务，并检查每个邮箱是否已在 accounts 表中注册
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    with get_db() as db:
        # 获取所有启用的 Outlook 服务
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        accounts = []
        registered_count = 0
        unregistered_count = 0

        for service in outlook_services:
            config = service.config or {}
            email = config.get("email") or service.name

            # 检查是否已注册（查询 accounts 表）
            existing_account = db.query(Account).filter(
                Account.email == email
            ).first()

            is_registered = existing_account is not None
            if is_registered:
                registered_count += 1
            else:
                unregistered_count += 1

            accounts.append(OutlookAccountForRegistration(
                id=service.id,
                email=email,
                name=service.name,
                has_oauth=bool(config.get("client_id") and config.get("refresh_token")),
                is_registered=is_registered,
                registered_account_id=existing_account.id if existing_account else None
            ))

        return OutlookAccountsListResponse(
            total=len(accounts),
            registered_count=registered_count,
            unregistered_count=unregistered_count,
            accounts=accounts
        )


async def run_outlook_batch_registration(
    batch_id: str,
    service_ids: List[int],
    skip_registered: bool,
    proxy: Optional[str],
    interval_min: int,
    interval_max: int,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
):
    """
    异步执行 Outlook 批量注册任务，复用通用并发逻辑

    将每个 service_id 映射为一个独立的 task_uuid，然后调用
    run_batch_registration 的并发逻辑
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 预先为每个 service_id 创建注册任务记录
    task_uuids = []
    with get_db() as db:
        for service_id in service_ids:
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=proxy,
                email_service_id=service_id
            )
            task_uuids.append(task_uuid)

    # 复用通用并发逻辑（outlook 服务类型，每个任务通过 email_service_id 定位账户）
    await run_batch_registration(
        batch_id=batch_id,
        task_uuids=task_uuids,
        email_service_type="outlook",
        proxy=proxy,
        email_service_config=None,
        email_service_id=None,   # 每个任务已绑定了独立的 email_service_id
        interval_min=interval_min,
        interval_max=interval_max,
        concurrency=concurrency,
        mode=mode,
        auto_upload_cpa=auto_upload_cpa,
        cpa_service_ids=cpa_service_ids,
        auto_upload_sub2api=auto_upload_sub2api,
        sub2api_service_ids=sub2api_service_ids,
        auto_upload_tm=auto_upload_tm,
        tm_service_ids=tm_service_ids,
    )


@router.post("/outlook-batch", response_model=OutlookBatchRegistrationResponse)
async def start_outlook_batch_registration(
    request: OutlookBatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    """
    启动 Outlook 批量注册任务

    - service_ids: 选中的 EmailService ID 列表
    - skip_registered: 是否自动跳过已注册邮箱（默认 True）
    - proxy: 代理地址
    - interval_min: 最小间隔秒数
    - interval_max: 最大间隔秒数
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    # 验证参数
    if not request.service_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个 Outlook 账户")

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    # 过滤掉已注册的邮箱
    actual_service_ids = request.service_ids
    skipped_count = 0

    if request.skip_registered:
        actual_service_ids = []
        with get_db() as db:
            for service_id in request.service_ids:
                service = db.query(EmailServiceModel).filter(
                    EmailServiceModel.id == service_id
                ).first()

                if not service:
                    continue

                config = service.config or {}
                email = config.get("email") or service.name

                # 检查是否已注册
                existing_account = db.query(Account).filter(
                    Account.email == email
                ).first()

                if existing_account:
                    skipped_count += 1
                else:
                    actual_service_ids.append(service_id)

    if not actual_service_ids:
        return OutlookBatchRegistrationResponse(
            batch_id="",
            total=len(request.service_ids),
            skipped=skipped_count,
            to_register=0,
            service_ids=[]
        )

    # 创建批量任务
    batch_id = str(uuid.uuid4())

    # 初始化批量任务状态
    task_manager.init_batch(
        batch_id,
        len(actual_service_ids),
        skipped=skipped_count,
        service_ids=actual_service_ids,
    )

    # 在后台运行批量注册
    background_tasks.add_task(
        run_outlook_batch_registration,
        batch_id,
        actual_service_ids,
        request.skip_registered,
        request.proxy,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
    )

    return OutlookBatchRegistrationResponse(
        batch_id=batch_id,
        total=len(request.service_ids),
        skipped=skipped_count,
        to_register=len(actual_service_ids),
        service_ids=actual_service_ids
    )


@router.get("/outlook-batch/{batch_id}")
async def get_outlook_batch_status(batch_id: str):
    """获取 Outlook 批量任务状态"""
    batch = _require_batch_snapshot(batch_id)
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "skipped": batch.get("skipped", 0),
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "service_ids": batch.get("service_ids", []),
        "logs": task_manager.get_batch_logs(batch_id),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.post("/outlook-batch/{batch_id}/cancel")
async def cancel_outlook_batch(batch_id: str):
    """取消 Outlook 批量任务"""
    batch = _require_batch_snapshot(batch_id)
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    task_manager.cancel_batch(batch_id)

    return {"success": True, "message": "批量任务取消请求已提交"}
