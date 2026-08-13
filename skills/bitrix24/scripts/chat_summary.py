#!/usr/bin/env python3
"""Fetch Bitrix24 chat messages for later LLM summarization.

Usage:
  python3 chat_summary.py "УД 01" [--days 14] [--limit 200] [--json]
  python3 chat_summary.py --chat-id 123 [--days 7] [--json]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from bitrix_client import (  # noqa: E402
    BitrixError,
    as_list,
    call,
    days_ago,
    emit_error,
    emit_json,
    now_utc,
    parse_dt,
    resolve_users,
)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _contains(haystack: Any, needle: str) -> bool:
    h, n = _norm(haystack), _norm(needle)
    return bool(n) and n in h


def dialog_id_for_chat(chat: dict[str, Any]) -> str:
    raw = (
        chat.get("dialog_id")
        or chat.get("dialogId")
        or chat.get("id")
        or chat.get("chat_id")
        or chat.get("chatId")
    )
    if raw is None:
        raise BitrixError(f"Chat has no id: {chat}")
    text = str(raw)
    if text.startswith(("chat", "sg")):
        return text
    entity = str(chat.get("entity_type") or chat.get("entityType") or chat.get("type") or "")
    if entity in ("sonet_group", "workgroup", "group", "project") or chat.get("source") == "sonet_group":
        return f"sg{text}"
    return f"chat{text}"


def chat_title(chat: dict[str, Any]) -> str:
    return str(
        chat.get("title")
        or chat.get("name")
        or chat.get("NAME")
        or ""
    ).strip()


def _from_search(query: str, limit: int) -> list[dict[str, Any]]:
    result = call("im.search.chat.list", {"FIND": query, "OFFSET": 0, "LIMIT": min(limit, 50)})
    chats = []
    for item in as_list(result):
        if isinstance(item, dict):
            item = dict(item)
            item.setdefault("source", "im.search.chat.list")
            chats.append(item)
    return chats


def _from_groups(query: str) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    try:
        result = call(
            "sonet_group.get",
            {
                "ORDER": {"NAME": "ASC"},
                "FILTER": {"%NAME": query, "ACTIVE": "Y"},
                "IS_ADMIN": "Y",
            },
        )
    except BitrixError:
        return chats
    for group in as_list(result):
        gid = group.get("ID") or group.get("id")
        name = group.get("NAME") or group.get("name")
        if gid is None:
            continue
        chats.append(
            {
                "id": gid,
                "dialog_id": f"sg{gid}",
                "title": name,
                "name": name,
                "type": "sonet_group",
                "entity_type": "SONET_GROUP",
                "source": "sonet_group.get",
            }
        )
    return chats


def _from_workgroups(query: str) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    try:
        result = call(
            "socialnetwork.api.workgroup.list",
            {
                "filter": {"%NAME": query},
                "select": ["ID", "NAME"],
                "shouldSelectDialogId": "Y",
                "params": {"IS_ADMIN": "Y"},
            },
        )
    except BitrixError:
        return chats
    items = result
    if isinstance(result, dict):
        items = result.get("workgroups") or result.get("items") or result.get("list") or result
    for group in as_list(items):
        gid = group.get("ID") or group.get("id")
        name = group.get("NAME") or group.get("name")
        dialog_id = group.get("dialogId") or group.get("dialog_id") or (f"sg{gid}" if gid else None)
        if not dialog_id:
            continue
        chats.append(
            {
                "id": gid,
                "dialog_id": dialog_id,
                "title": name,
                "name": name,
                "type": "workgroup",
                "source": "socialnetwork.api.workgroup.list",
            }
        )
    return chats


def _from_recent(query: str, pages: int = 4) -> list[dict[str, Any]]:
    chats: list[dict[str, Any]] = []
    last_date = None
    try:
        for _ in range(pages):
            params: dict[str, Any] = {
                "SKIP_OPENLINES": "Y",
                "SKIP_DIALOG": "Y",
                "LIMIT": 50,
            }
            if last_date:
                params["LAST_MESSAGE_DATE"] = last_date
            result = call("im.recent.list", params)
            items = result
            if isinstance(result, dict):
                items = result.get("items") or result.get("list") or result
            batch = as_list(items)
            if not batch:
                break
            for item in batch:
                title = item.get("title") or item.get("name") or ""
                if not _contains(title, query):
                    continue
                dialog_id = item.get("id") or item.get("dialog_id") or item.get("dialogId")
                chat_id = item.get("chat_id") or item.get("chatId")
                item = dict(item)
                item["dialog_id"] = dialog_id
                item["id"] = chat_id or dialog_id
                item["title"] = title
                item["source"] = "im.recent.list"
                chats.append(item)
            dates = [it.get("date_last_activity") or it.get("message", {}).get("date") if isinstance(it.get("message"), dict) else it.get("date") for it in batch]
            dates = [d for d in dates if d]
            if not dates:
                break
            last_date = min(str(d) for d in dates)
            if len(batch) < 50:
                break
    except BitrixError:
        return chats
    return chats


def find_chats(query: str, limit: int = 10) -> tuple[list[dict[str, Any]], list[str]]:
    tried: list[str] = []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(items: list[dict[str, Any]], source: str) -> None:
        tried.append(source)
        for item in items:
            try:
                did = dialog_id_for_chat(item)
            except BitrixError:
                continue
            title = chat_title(item)
            if query and title and not _contains(title, query) and source != "im.search.chat.list":
                continue
            if did in seen:
                continue
            seen.add(did)
            found.append(item)

    add(_from_search(query, limit), "im.search.chat.list")
    if not found:
        add(_from_groups(query), "sonet_group.get")
    if not found:
        add(_from_workgroups(query), "socialnetwork.api.workgroup.list")
    if not found:
        add(_from_recent(query), "im.recent.list")
    return found[:limit], tried


def fetch_messages(
    dialog_id: str,
    *,
    since: datetime | None,
    max_messages: int,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    last_id: int | None = None

    while len(messages) < max_messages:
        params: dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "LIMIT": min(page_size, max_messages - len(messages)),
        }
        if last_id is not None:
            params["LAST_ID"] = last_id

        result = call("im.dialog.messages.get", params)
        batch = []
        if isinstance(result, dict):
            batch = result.get("messages") or []
        elif isinstance(result, list):
            batch = result

        if not batch:
            break

        stop = False
        for msg in batch:
            dt = parse_dt(msg.get("date"))
            if since and dt and dt < since:
                stop = True
                continue
            messages.append(msg)

        ids = [int(m["id"]) for m in batch if m.get("id") is not None]
        if not ids:
            break
        next_last = min(ids)
        if last_id is not None and next_last >= last_id:
            break
        last_id = next_last
        if stop or len(batch) < params["LIMIT"]:
            break

    messages.sort(key=lambda m: (m.get("date") or "", m.get("id") or 0))
    return messages[:max_messages]


def normalize_message(msg: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    author_id = str(msg.get("author_id") or msg.get("senderId") or "0")
    text = (msg.get("text") or msg.get("message") or "").strip()
    return {
        "id": msg.get("id"),
        "date": msg.get("date"),
        "author_id": author_id,
        "author": names.get(author_id) or ("system" if author_id == "0" else author_id),
        "text": text,
    }


def pick_chat(chats: list[dict[str, Any]], query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not chats:
        return None, []
    q = _norm(query)
    exact = [c for c in chats if _norm(chat_title(c)) == q]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    if len(chats) == 1:
        return chats[0], []
    return None, chats


def chat_brief(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": chat.get("id") or chat.get("chat_id") or chat.get("chatId"),
        "dialog_id": dialog_id_for_chat(chat),
        "title": chat_title(chat) or None,
        "type": chat.get("type") or chat.get("entity_type") or chat.get("entityType"),
        "source": chat.get("source"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitrix24 chat messages for summary")
    parser.add_argument("query", nargs="?", help="Chat title substring, e.g. УД 01")
    parser.add_argument("--chat-id", help="Numeric chat id or dialog id (chat123 / sg45)")
    parser.add_argument("--days", type=int, default=14, help="Look back N days (default 14)")
    parser.add_argument("--limit", type=int, default=200, help="Max messages to fetch")
    parser.add_argument("--json", action="store_true", help="JSON output (default)")
    args = parser.parse_args()

    if not args.query and not args.chat_id:
        emit_error("Provide chat title query or --chat-id")

    try:
        since = days_ago(args.days) if args.days > 0 else None
        selected: dict[str, Any] | None = None
        candidates: list[dict[str, Any]] = []
        tried: list[str] = []

        if args.chat_id:
            cid = str(args.chat_id)
            dialog_id = cid if cid.startswith(("chat", "sg")) else f"chat{cid}"
            selected = {"id": cid, "dialog_id": dialog_id, "title": None}
        else:
            found, tried = find_chats(args.query or "", limit=10)
            selected, candidates = pick_chat(found, args.query or "")
            if selected is None:
                emit_json(
                    {
                        "ok": False,
                        "error": "ambiguous_or_missing_chat",
                        "query": args.query,
                        "tried": tried,
                        "hint": (
                            "If this is a project/group chat, try the exact title "
                            "or --chat-id sg<GROUP_ID>. Incoming webhook must have "
                            "im + socialnetwork/sonet scopes, and the webhook user "
                            "must be a member of the chat."
                        ),
                        "candidates": [chat_brief(c) for c in candidates],
                    },
                    exit_code=2,
                )

        assert selected is not None
        dialog_id = selected.get("dialog_id") or dialog_id_for_chat(selected)
        raw_messages = fetch_messages(
            dialog_id,
            since=since,
            max_messages=max(1, args.limit),
        )
        author_ids = [m.get("author_id") or m.get("senderId") for m in raw_messages]
        names = resolve_users([a for a in author_ids if a is not None])
        messages = [
            normalize_message(m, names)
            for m in raw_messages
            if (m.get("text") or m.get("message") or "").strip()
        ]

        emit_json(
            {
                "ok": True,
                "query": args.query,
                "tried": tried,
                "chat": {
                    "id": selected.get("id") or selected.get("chat_id"),
                    "dialog_id": dialog_id,
                    "title": chat_title(selected) or None,
                    "source": selected.get("source"),
                },
                "period": {
                    "days": args.days,
                    "from": since.isoformat() if since else None,
                    "to": now_utc().isoformat(),
                },
                "message_count": len(messages),
                "messages": messages,
                "instruction": (
                    "Summarize substantial discussion: decisions, open questions, "
                    "risks, action items. Ignore greetings/noise. Cite authors/dates."
                ),
            }
        )
    except BitrixError as exc:
        emit_error(str(exc))


if __name__ == "__main__":
    main()
