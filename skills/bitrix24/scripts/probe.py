#!/usr/bin/env python3
"""Bitrix24 connectivity / scope probe. Prints what the webhook can see.

Usage:
  python3 probe.py
  python3 probe.py --find "УД"
  python3 probe.py --find-task "сценариев"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from bitrix_client import BitrixError, as_list, call, call_full  # noqa: E402


def _show(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


def try_call(method: str, params: dict[str, Any] | None = None) -> Any:
    try:
        full = call_full(method, params)
        return {"ok": True, "result": full.get("result"), "total": full.get("total")}
    except BitrixError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Bitrix24 webhook access")
    parser.add_argument("--find", help="Substring to search in groups/chats")
    parser.add_argument("--find-task", help="Substring to search in task titles")
    args = parser.parse_args()

    _show("profile", try_call("profile"))
    _show("app.info", try_call("app.info"))

    needle = (args.find or "").strip()
    if needle:
        _show(
            f"im.search.chat.list FIND={needle!r}",
            try_call("im.search.chat.list", {"FIND": needle, "LIMIT": 10}),
        )
        _show(
            f"sonet_group.get %NAME={needle!r}",
            try_call(
                "sonet_group.get",
                {"FILTER": {"%NAME": needle, "ACTIVE": "Y"}, "IS_ADMIN": "Y"},
            ),
        )
        _show(
            f"socialnetwork.api.workgroup.list %NAME={needle!r}",
            try_call(
                "socialnetwork.api.workgroup.list",
                {
                    "filter": {"%NAME": needle},
                    "select": ["ID", "NAME"],
                    "shouldSelectDialogId": "Y",
                    "params": {"IS_ADMIN": "Y"},
                },
            ),
        )

    recent = try_call(
        "im.recent.list",
        {"SKIP_OPENLINES": "Y", "SKIP_DIALOG": "Y", "LIMIT": 20},
    )
    if recent.get("ok") and isinstance(recent.get("result"), dict):
        items = as_list(recent["result"].get("items") or recent["result"])
        slim = []
        for it in items[:20]:
            slim.append(
                {
                    "id": it.get("id"),
                    "chat_id": it.get("chat_id") or it.get("chatId"),
                    "title": it.get("title") or it.get("name"),
                    "type": it.get("type"),
                }
            )
        _show("im.recent.list (group chats sample)", {"ok": True, "items": slim})
    else:
        _show("im.recent.list", recent)

    groups = try_call(
        "sonet_group.get",
        {"ORDER": {"NAME": "ASC"}, "FILTER": {"ACTIVE": "Y"}, "IS_ADMIN": "Y"},
    )
    if groups.get("ok"):
        slim = []
        for g in as_list(groups.get("result"))[:30]:
            slim.append(
                {
                    "ID": g.get("ID") or g.get("id"),
                    "NAME": g.get("NAME") or g.get("name"),
                    "dialog_id": f"sg{g.get('ID') or g.get('id')}",
                }
            )
        _show("sonet_group.get (first 30)", {"ok": True, "groups": slim, "total": groups.get("total")})
    else:
        _show("sonet_group.get", groups)

    task_q = (args.find_task or "").strip()
    if task_q:
        for label, filt in (
            ("TITLE %pattern%", {"TITLE": f"%{task_q}%"}),
            ("%TITLE", {"%TITLE": task_q}),
        ):
            raw = try_call(
                "tasks.task.list",
                {
                    "filter": filt,
                    "select": ["ID", "TITLE", "STATUS", "GROUP_ID"],
                    "order": {"ID": "desc"},
                    "start": 0,
                },
            )
            if raw.get("ok") and isinstance(raw.get("result"), dict):
                tasks = as_list(raw["result"].get("tasks"))
                slim = [
                    {
                        "id": t.get("id") or t.get("ID"),
                        "title": t.get("title") or t.get("TITLE"),
                        "status": t.get("status") or t.get("STATUS"),
                    }
                    for t in tasks[:15]
                    if isinstance(t, dict)
                ]
                _show(f"tasks.task.list [{label}]", {"ok": True, "tasks": slim, "total": raw.get("total")})
            else:
                _show(f"tasks.task.list [{label}]", raw)

    # Quick scope hint
    print(
        "\nHint: if sonet_group.get / workgroup.list fail with insufficient_scope, "
        "add «Соцсеть / Группы» to the incoming webhook and recreate the URL."
    )


if __name__ == "__main__":
    main()
