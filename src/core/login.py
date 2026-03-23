"""
登录流程引擎
从 register.py 中拆分的登录专属方法
"""

import urllib.parse
import base64
import json as json_module
import time
from datetime import datetime
from typing import Optional, Dict, Any

from .register import RegistrationEngine, RegistrationResult
from ..config.constants import OPENAI_API_ENDPOINTS


class LoginEngine(RegistrationEngine):
    """
    登录引擎
    继承 RegistrationEngine，包含登录流程专属方法：
    - _follow_login_redirects
    - _submit_login_form
    - _send_verification_code_passwordless
    - _get_workspace_id
    - _select_workspace
    - _follow_redirects
    - _handle_oauth_callback
    """

    def _follow_login_redirects(self, start_url: str) -> bool:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code == 200:
                    self._log(f"非重定向状态码: {response.status_code}")
                    return True

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")

                current_url = next_url

            self._log("未能在重定向链中找到最终 URL")
            return False

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return False

    def _submit_login_form(self, did: str, sen_token) -> bool:
        """处理 免密登录"""
        try:
            self._log("处理免密登录...")
            login_body = f'{{"username":{{"value":"{self.email}","kind":"email"}}}}'
            headers = {
                "referer": "https://auth.openai.com/log-in",
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = f'{{"p": "", "t": "", "c": "{sen_token}", "id": "{did}", "flow": "authorize_continue"}}'
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=login_body,
            )
            self._log(f"提交登录表单状态: {response.status_code}")
            if response.status_code == 200:
                return True
            return False

        except Exception as e:
            self._log(f"处理登录失败: {e}", "error")
            return False

    def _send_verification_code_passwordless(self) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()
            response = self.session.post(
                OPENAI_API_ENDPOINTS["passwordless_send_otp"],
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json"
                }
            )

            self._log(f"验证码发送状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _decode_workspace_id(self, auth_cookie: str) -> str:
        """从授权 Cookie 中解析 Workspace ID"""
        segments = auth_cookie.split(".")
        if len(segments) < 1:
            raise ValueError("授权 Cookie 格式错误")

        payload = segments[0]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
        auth_json = json_module.loads(decoded.decode("utf-8"))

        workspaces = auth_json.get("workspaces") or []
        if not workspaces:
            raise ValueError("授权 Cookie 里没有 workspace 信息")

        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        if not workspace_id:
            raise ValueError("无法解析 workspace_id")

        return workspace_id

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        backoff_seconds = (1, 2, 4)
        max_attempts = len(backoff_seconds) + 1

        for attempt in range(1, max_attempts + 1):
            try:
                auth_cookie = self.session.cookies.get("oai-client-auth-session")
                if auth_cookie:
                    workspace_id = self._decode_workspace_id(auth_cookie)
                    self._log(f"Workspace ID: {workspace_id}")
                    return workspace_id

                raise ValueError("未能获取到授权 Cookie")
            except Exception as e:
                level = "warning" if attempt < max_attempts else "error"
                self._log(
                    f"获取 Workspace ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    level,
                )

            if attempt < max_attempts:
                wait_seconds = backoff_seconds[attempt - 1]
                self._log(f"等待 {wait_seconds} 秒后重试 Workspace ID", "warning")
                time.sleep(wait_seconds)

        return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                },
                data=select_body,
            )

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._log("1. 检查 IP 地理位置...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._log("2. 创建邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            # 3. 初始化会话
            self._log("3. 初始化会话...")
            if not self._init_session():
                result.error_message = "初始化会话失败"
                return result

            # 4. 开始 OAuth 流程
            self._log("4. 开始 OAuth 授权流程...")
            if not self._start_oauth():
                result.error_message = "开始 OAuth 流程失败"
                return result

            # 5. 获取 Device ID
            self._log("5. 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result

            # 6. 检查 Sentinel 拦截
            self._log("6. 检查 Sentinel 拦截...")
            sen_token = self._check_sentinel(did)
            if sen_token:
                self._log("Sentinel 检查通过")
            else:
                self._log("Sentinel 检查失败或未启用", "warning")

            # 7. 提交注册表单 + 解析响应判断账号状态
            self._log("7. 提交注册表单...")
            signup_result = self._submit_signup_form(did, sen_token)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return result

            # 8. 检测到已注册账号 → 直接终止任务
            if self._is_existing_account:
                self._log(f"8. 邮箱 {self.email} 在 OpenAI 已注册，跳过注册流程", "warning")
                result.error_message = f"邮箱 {self.email} 已在 OpenAI 注册"
                return result
            else:
                self._log("8. 注册密码...")
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result

            # 9. 发送验证码
            self._log("9. 发送验证码...")
            if not self._send_verification_code():
                result.error_message = "发送验证码失败"
                return result

            # 10. 获取验证码（超时后重发一次）
            self._log("10. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                self._log("10. 验证码超时，重新发送...")
                if self._send_verification_code():
                    code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return result

            # 11. 验证验证码
            self._log("11. 验证验证码...")
            if not self._validate_verification_code(code):
                result.error_message = "验证验证码失败"
                return result

            # 12. 创建用户账户
            self._log("12. 创建用户账户...")
            if not self._create_user_account():
                result.error_message = "创建用户账户失败"
                return result

            self._log("13-1. 结束注册,启用登录流程...")
            if not self._follow_login_redirects(self.oauth_start.auth_url):
                result.error_message = "跟随重定向链失败"
                return result

            self._log("13-2. 提交登陆表单")
            if not self._submit_login_form(did, sen_token):
                result.error_message = "提交登陆表单失败"
                return result

            self._log("14. 发送验证码...")
            if not self._send_verification_code_passwordless():
                result.error_message = "发送验证码失败"
                return result

            self._log("15. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                self._log("15. 验证码超时，重新发送...")
                if self._send_verification_code_passwordless():
                    code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return result

            self._log("16. 验证验证码...")
            if not self._validate_verification_code(code):
                result.error_message = "验证验证码失败"
                return result

            # 13. 获取 Workspace ID
            self._log("17. 获取 Workspace ID...")
            workspace_id = self._get_workspace_id()
            if not workspace_id:
                result.error_message = "获取 Workspace ID 失败"
                return result

            result.workspace_id = workspace_id

            # 14. 选择 Workspace
            self._log("18. 选择 Workspace...")
            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                result.error_message = "选择 Workspace 失败"
                return result

            # 15. 跟随重定向链
            self._log("19. 跟随重定向链...")
            callback_url = self._follow_redirects(continue_url)
            if not callback_url:
                result.error_message = "跟随重定向链失败"
                return result

            # 16. 处理 OAuth 回调
            self._log("20. 处理 OAuth 回调...")
            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "处理 OAuth 回调失败"
                return result

            # 提取账户信息
            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.password = self.password or ""  # 保存密码（已注册账号为空）

            # 设置来源标记
            result.source = "register"

            # 尝试获取 session_token 从 cookie
            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = session_cookie
                result.session_token = session_cookie
                self._log(f"获取到 Session Token")

            # 17. 完成
            self._log("=" * 60)
            self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result
        finally:
            self.close()
