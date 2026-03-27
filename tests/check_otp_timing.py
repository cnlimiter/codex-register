#!/usr/bin/env python3
"""
离线验证 TempmailService 的 OTP 时间锚点过滤行为。

场景 1:
- 30 秒内先后收到两封邮件
- 在两封邮件之间设置新的 otp_sent_at
- 期望过滤第一封，命中第二封

场景 2:
- 第二封邮件已经入箱后才刷新 otp_sent_at
- 期望复现严格时间过滤导致第二封也被排除的窗口
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional

import src.services.tempmail as tempmail_module
from src.services.tempmail import TempmailService


@dataclass(frozen=True)
class Scenario:
    name: str
    anchor_offset_seconds: int
    expected_code: Optional[str]
    expected_message: str


class FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._payload


class FakeHTTPClient:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(self.payload)


class FakeClock:
    def __init__(self, start: float):
        self.current = float(start)

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += float(seconds)


def build_inbox_payload(base_timestamp: int) -> Dict[str, Any]:
    return {
        "emails": [
            {
                "id": "mail-1",
                "received_at": base_timestamp + 10,
                "from": "noreply@openai.com",
                "subject": "First OTP",
                "body": "111111",
            },
            {
                "id": "mail-2",
                "received_at": base_timestamp + 20,
                "from": "noreply@openai.com",
                "subject": "Second OTP",
                "body": "222222",
            },
        ]
    }


def run_scenario(scenario: Scenario) -> Dict[str, Any]:
    base_timestamp = 1_700_000_000
    service = TempmailService({"base_url": "https://api.tempmail.test"})
    service._email_cache["tester@example.com"] = {"token": "token-1"}
    service.http_client = FakeHTTPClient(build_inbox_payload(base_timestamp))

    fake_clock = FakeClock(start=base_timestamp + scenario.anchor_offset_seconds)
    anchor_timestamp = fake_clock.time()
    original_time = tempmail_module.time.time
    original_sleep = tempmail_module.time.sleep

    try:
        tempmail_module.time.time = fake_clock.time
        tempmail_module.time.sleep = fake_clock.sleep
        code = service.get_verification_code(
            email="tester@example.com",
            timeout=1,
            otp_sent_at=anchor_timestamp,
        )
    finally:
        tempmail_module.time.time = original_time
        tempmail_module.time.sleep = original_sleep

    passed = code == scenario.expected_code
    return {
        "name": scenario.name,
        "anchor_timestamp": anchor_timestamp,
        "code": code,
        "passed": passed,
        "http_calls": len(service.http_client.calls),
        "message": scenario.expected_message,
    }


def main() -> int:
    logging.getLogger("src.services.tempmail").setLevel(logging.ERROR)

    scenarios = [
        Scenario(
            name="anchor_between_two_emails",
            anchor_offset_seconds=15,
            expected_code="222222",
            expected_message="新锚点位于两封邮件之间，第一封应被过滤，第二封应被命中。",
        ),
        Scenario(
            name="anchor_set_after_second_email",
            anchor_offset_seconds=21,
            expected_code=None,
            expected_message="锚点晚于第二封邮件时，严格大于过滤会把第二封也排除，复现登录阶段的竞态窗口。",
        ),
    ]

    print("Tempmail OTP timing check")
    print("=========================")

    failed = False
    for scenario in scenarios:
        result = run_scenario(scenario)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {result['name']}")
        print(f"  anchor_timestamp={result['anchor_timestamp']}")
        print(f"  returned_code={result['code']}")
        print(f"  inbox_polls={result['http_calls']}")
        print(f"  note={result['message']}")
        if not result["passed"]:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
