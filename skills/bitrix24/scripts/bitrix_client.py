#!/usr/bin/env python3
"""Minimal Bitrix24 REST client for incoming webhooks."""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


class BitrixError(RuntimeError):
    """Bitrix24 REST call failed."""


def webhook_base() -> str:
    """
    Resolve webhook base URL.

    Preferred:
      BITRIX24_WEBHOOK_URL=https://portal.bitrix24.ru/rest/1/xxxxx/

    Or parts:
      BITRIX24_PORTAL=https://portal.bitrix24.ru
      BITRIX24_USER_ID=1
      BITRIX24_WEBHOOK_CODE=xxxxx
    """
    url = (os.environ.get("BITRIX24_WEBHOOK_URL") or "").strip().rstrip("/")
    if url:
        return url + "/"

    portal = (os.environ.get("BITRIX24_PORTAL") or "").strip().rstrip("/")
    user_id = (os.environ.get("BITRIX24_USER_ID") or "").strip()
    code = (os.environ.get("BITRIX24_WEBHOOK_CODE") or "").strip()
    if portal and user_id and code:
        return f"{portal}/rest/{user_id}/{code}/"

    raise BitrixError(
        "Set BITRIX24_WEBHOOK_URL "
        "(or BITRIX24_PORTAL + BITRIX24_USER_ID + BITRIX24_WEBHOOK_CODE)"
    )


def call(method: str, params: dict[str, Any] | None = None) -> Any:
    """Call Bitrix24 REST method. Returns `result` field."""
    base = webhook_base()
    url = urllib.parse.urljoin(base, method + ".json")
    body = urllib.parse.urlencode(_flatten(params or {}), doseq=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BitrixError(f"HTTP {exc.code} on {method}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BitrixError(f"Network error on {method}: {exc}") from exc

    if "error" in payload:
        raise BitrixError(
            f"{payload.get('error')}: {payload.get('error_description') or payload}"
        )
    return payload.get("result")


def _flatten(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dict/list into Bitrix form encoding."""
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        full = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            items.extend(_flatten(value, full))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    items.extend(_flatten(item, f"{full}[{i}]"))
                else:
                    items.append((f"{full}[{i}]", _scalar(item)))
        elif value is None:
            continue
        else:
            items.append((full, _scalar(value)))
    return items


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days_ago(days: int) -> datetime:
    return now_utc() - timedelta(days=days)


def format_hours(seconds: int | float) -> float:
    return round(float(seconds) / 3600.0, 2)


def resolve_users(user_ids: list[str | int]) -> dict[str, str]:
    """Map user id -> display name."""
    ids = sorted({str(uid) for uid in user_ids if str(uid) not in ("", "0")})
    if not ids:
        return {}
    result = call("user.get", {"FILTER": {"ID": ids}})
    names: dict[str, str] = {}
    if not isinstance(result, list):
        return names
    for user in result:
        uid = str(user.get("ID") or "")
        parts = [
            user.get("NAME") or "",
            user.get("LAST_NAME") or "",
        ]
        name = " ".join(p for p in parts if p).strip()
        if not name:
            name = user.get("EMAIL") or user.get("LOGIN") or uid
        names[uid] = name
    return names


def emit_json(data: Any, *, exit_code: int = 0) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    raise SystemExit(exit_code)


def emit_error(message: str, *, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    emit_json(payload, exit_code=1)
