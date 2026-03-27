"""
支付核心逻辑 — 生成 Plus/Team 支付链接、无痕打开浏览器、检测订阅状态
"""

import logging
import subprocess
import sys
import uuid
from typing import Optional

from curl_cffi import requests as cffi_requests

from ...database.models import Account

logger = logging.getLogger(__name__)

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
TEAM_CHECKOUT_BASE_URL = "https://chatgpt.com/checkout/openai_llc/"


def _build_proxies(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


_COUNTRY_CURRENCY_MAP = {
    "SG": "SGD",
    "US": "USD",
    "TR": "TRY",
    "JP": "JPY",
    "HK": "HKD",
    "GB": "GBP",
    "EU": "EUR",
    "AU": "AUD",
    "CA": "CAD",
    "IN": "INR",
    "BR": "BRL",
    "MX": "MXN",
}


def _extract_oai_did(cookies_str: str) -> Optional[str]:
    """从 cookie 字符串中提取 oai-device-id"""
    for part in cookies_str.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            return part[len("oai-did="):].strip()
    return None


def _parse_cookie_str(cookies_str: str, domain: str) -> list:
    """将 'key=val; key2=val2' 格式解析为 Playwright cookie 列表"""
    cookies = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
        })
    return cookies


def _open_url_system_browser(url: str) -> bool:
    """回退方案：调用系统浏览器以无痕模式打开"""
    platform = sys.platform
    try:
        if platform == "win32":
            for browser, flag in [("chrome", "--incognito"), ("msedge", "--inprivate")]:
                try:
                    subprocess.Popen(f'start {browser} {flag} "{url}"', shell=True)
                    return True
                except Exception:
                    continue
        elif platform == "darwin":
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", "--incognito", url])
            return True
        else:
            for binary in ["google-chrome", "chromium-browser", "chromium"]:
                try:
                    subprocess.Popen([binary, "--incognito", url])
                    return True
                except FileNotFoundError:
                    continue
    except Exception as e:
        logger.warning(f"系统浏览器无痕打开失败: {e}")
    return False


def generate_plus_link(
    account: Account,
    proxy: Optional[str] = None,
    country: str = "SG",
) -> str:
    """生成 Plus 支付链接（后端携带账号 cookie 发请求）"""
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
    }
    if account.cookies:
        headers["cookie"] = account.cookies
        oai_did = _extract_oai_did(account.cookies)
        if oai_did:
            headers["oai-device-id"] = oai_did

    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }

    resp = cffi_requests.post(
        PAYMENT_CHECKOUT_URL,
        headers=headers,
        json=payload,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()
    if "checkout_session_id" in data:
        return TEAM_CHECKOUT_BASE_URL + data["checkout_session_id"]
    raise ValueError(data.get("detail", "API 未返回 checkout_session_id"))


def generate_team_link(
    account: Account,
    workspace_name: str = "MyTeam",
    price_interval: str = "month",
    seat_quantity: int = 5,
    proxy: Optional[str] = None,
    country: str = "SG",
) -> str:
    """生成 Team 支付链接（后端携带账号 cookie 发请求）"""
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    currency = _COUNTRY_CURRENCY_MAP.get(country, "USD")
    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
        "oai-device-id": str(uuid.uuid4()),
    }

    payload = {
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": workspace_name,
            "price_interval": price_interval,
            "seat_quantity": seat_quantity,
        },
        "billing_details": {"country": country, "currency": currency},
        "promo_campaign": {
            "promo_campaign_id": "team-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
    }

    resp = cffi_requests.post(
        PAYMENT_CHECKOUT_URL,
        headers=headers,
        json=payload,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    resp.raise_for_status()
    data = resp.json()
    resp2 = cffi_requests.post(
        "https://api.stripe.com/v1/payment_pages/" + data["checkout_session_id"],
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "accept": "application/json",
            "referer": "https://js.stripe.com/"
        },
        data=f"tax_region[country]={country}"
             "&elements_session_client[client_betas][0]=custom_checkout_server_updates_1"
             "&elements_session_client[client_betas][1]=custom_checkout_manual_approval_1"
             "&elements_session_client[elements_init_source]=custom_checkout"
             "&elements_session_client[referrer_host]=chatgpt.com"
             "&elements_session_client[session_id]=elements_session_1rr8sS4PKIY"
             "&elements_session_client[stripe_js_id]=72d6a553-c2fb-4f85-941e-8022c8335a85"
             "&elements_session_client[locale]=zh"
             "&elements_session_client[is_aggregation_expected]=false"
             "&client_attribution_metadata[merchant_integration_additional_elements][0]=payment"
             "&client_attribution_metadata[merchant_integration_additional_elements][1]=address"
             f"&key={data["publishable_key"]}"
        ,
        proxies=_build_proxies(proxy),
        timeout=30,
        impersonate="chrome110",
    )
    resp2.raise_for_status()
    data2 = resp2.json()
    if "stripe_hosted_url" in data2:
        return data2["stripe_hosted_url"]
    raise ValueError(data.get("detail", "API 未返回 stripe_hosted_url"))


def open_url_incognito(url: str, cookies_str: Optional[str] = None) -> bool:
    """用 Playwright 以无痕模式打开 URL，可注入 cookie"""
    import threading
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright 未安装，回退到系统浏览器")
        return _open_url_system_browser(url)

    def _launch():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, args=["--incognito"])
                ctx = browser.new_context()
                if cookies_str:
                    ctx.add_cookies(_parse_cookie_str(cookies_str, "chatgpt.com"))
                page = ctx.new_page()
                page.goto(url)
                # 保持窗口打开直到用户关闭
                page.wait_for_timeout(300_000)  # 最多等待 5 分钟
        except Exception as e:
            logger.warning(f"Playwright 无痕打开失败: {e}")

    threading.Thread(target=_launch, daemon=True).start()
    return True


def check_subscription_status(account: Account, proxy: Optional[str] = None) -> str:
    """
    检测账号当前订阅状态。

    Returns:
        'free' / 'plus' / 'team'
    """
    if not account.access_token:
        raise ValueError("账号缺少 access_token")

    headers = {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(2):
        try:
            resp = cffi_requests.get(
                "https://chatgpt.com/backend-api/me",
                headers=headers,
                proxies=_build_proxies(proxy),
                timeout=45,
                impersonate="chrome110",
            )

            if resp.status_code == 200:
                data = resp.json()

                # 解析订阅类型
                plan = data.get("plan_type") or ""
                if "team" in plan.lower():
                    return "team"
                if "plus" in plan.lower():
                    return "plus"

                # 尝试从 orgs 或 workspace 信息判断
                orgs = data.get("orgs", {}).get("data", [])
                for org in orgs:
                    settings_ = org.get("settings", {})
                    if settings_.get("workspace_plan_type") in ("team", "enterprise"):
                        return "team"

                return "free"

            if resp.status_code in (401, 403):
                resp.raise_for_status()

            last_error = f"HTTP {resp.status_code}"
            if resp.status_code >= 500 and attempt == 0:
                time.sleep(2)
                continue
            resp.raise_for_status()
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(2)
                continue
            raise

    raise RuntimeError(last_error or "subscription check failed")
