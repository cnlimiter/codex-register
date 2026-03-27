#!/usr/bin/env bash
set -euo pipefail

TARGET_MODE="${1:-}"
ROUTE_DEV="${ROUTE_DEV:-ens3}"
PROXY_GATEWAY="${PROXY_GATEWAY:-192.168.10.1}"
DEFAULT_GATEWAY="${DEFAULT_GATEWAY:-192.168.10.254}"
STATE_FILE="${STATE_FILE:-/tmp/codex-manager-gateway-state}"

if [[ "$TARGET_MODE" != "proxy" && "$TARGET_MODE" != "default" ]]; then
  echo "usage: $0 {proxy|default}" >&2
  exit 2
fi

if [[ "$TARGET_MODE" == "proxy" ]]; then
  echo "$DEFAULT_GATEWAY" > "$STATE_FILE"
  ip route replace default via "$PROXY_GATEWAY" dev "$ROUTE_DEV"
  echo "switched default route to $PROXY_GATEWAY dev $ROUTE_DEV"
else
  gw="$DEFAULT_GATEWAY"
  if [[ -f "$STATE_FILE" ]]; then
    gw=$(cat "$STATE_FILE" 2>/dev/null || echo "$DEFAULT_GATEWAY")
  fi
  ip route replace default via "$gw" dev "$ROUTE_DEV"
  echo "restored default route to $gw dev $ROUTE_DEV"
fi
