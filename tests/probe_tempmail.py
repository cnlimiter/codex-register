#!/usr/bin/env python3
"""
Tempmail.lol API 探针。

用途：
1. 创建测试收件箱或复用现有 token。
2. 拉取 /inbox 原始 JSON 并原样打印。
3. 检查邮件对象里是否存在 received_at/date 等时间字段。
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, Iterable

import httpx


DEFAULT_BASE_URL = "https://api.tempmail.lol/v2"
TIME_FIELDS = ("received_at", "date", "created_at", "createdAt", "timestamp")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 Tempmail.lol 收件箱原始 JSON")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Tempmail API 基础地址")
    parser.add_argument("--token", help="已有 inbox token；未提供时自动创建新邮箱")
    parser.add_argument("--poll-count", type=int, default=1, help="轮询次数")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="轮询间隔秒数")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP 超时时间")
    return parser.parse_args()


def dump_json(title: str, payload: Dict[str, Any]) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def summarize_time_fields(emails: Iterable[Dict[str, Any]]) -> None:
    for index, message in enumerate(emails, start=1):
        present_fields = {name: message.get(name) for name in TIME_FIELDS if name in message}
        print(f"email[{index}] 时间字段: {json.dumps(present_fields, ensure_ascii=False, default=str)}")


def create_inbox(client: httpx.Client, base_url: str) -> Dict[str, Any]:
    response = client.post(
        f"{base_url}/inbox/create",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={},
    )
    print(f"CREATE_STATUS {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    dump_json("CREATE_RESPONSE", payload)
    return payload


def fetch_inbox(client: httpx.Client, base_url: str, token: str) -> Dict[str, Any]:
    response = client.get(
        f"{base_url}/inbox",
        params={"token": token},
        headers={"Accept": "application/json"},
    )
    print(f"INBOX_STATUS {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    dump_json("INBOX_RESPONSE", payload)
    emails = payload.get("emails", []) if isinstance(payload, dict) else []
    if isinstance(emails, list):
        summarize_time_fields([mail for mail in emails if isinstance(mail, dict)])
    else:
        print(f"emails 字段不是列表: {type(emails).__name__}")
    return payload


def main() -> int:
    args = parse_args()
    with httpx.Client(timeout=args.timeout) as client:
        token = args.token
        if not token:
            inbox = create_inbox(client, args.base_url)
            token = str(inbox.get("token", "")).strip()
            address = str(inbox.get("address", "")).strip()
            print(f"ADDRESS {address}")
            print(f"TOKEN {token}")

        if not token:
            print("未拿到 token，无法继续拉取 inbox", file=sys.stderr)
            return 1

        for attempt in range(1, args.poll_count + 1):
            print(f"\n----- poll {attempt}/{args.poll_count} -----")
            fetch_inbox(client, args.base_url, token)
            if attempt < args.poll_count:
                time.sleep(args.poll_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
