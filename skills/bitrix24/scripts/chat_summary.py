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
    call,
    days_ago,
    emit_error,
    emit_json,
    now_utc,
    parse_dt,
    resolve_users,
)


def find_chats(query: str, limit: int = 10) -> list[dict[str, Any]]:
    result = call("im.search.chat.list", {"FIND": query, "LIMIT": limit})
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("items", "chats", "list"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def dialog_id_for_chat(chat: dict[str, Any]) -> str:
    raw = chat.get("dialog_id") or chat.get("id") or chat.get("chat_id")
    if raw is None:
        raise BitrixError(f"Chat has no id: {chat}")
    text = str(raw)
    if text.startswith(("chat", "sg")):
        return text
    return f"chat{text}"


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
    q = query.strip().casefold()
    exact = []
    for chat in chats:
        title = str(chat.get("title") or chat.get("name") or "").strip()
        if title.casefold() == q:
            exact.append(chat)
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    if len(chats) == 1:
        return chats[0], []
    return None, chats


def chat_brief(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": chat.get("id") or chat.get("chat_id"),
        "dialog_id": dialog_id_for_chat(chat),
        "title": chat.get("title") or chat.get("name"),
        "type": chat.get("type") or chat.get("entity_type"),
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

        if args.chat_id:
            cid = str(args.chat_id)
            dialog_id = cid if cid.startswith(("chat", "sg")) else f"chat{cid}"
            selected = {"id": cid, "dialog_id": dialog_id, "title": None}
        else:
            found = find_chats(args.query or "", limit=10)
            selected, candidates = pick_chat(found, args.query or "")
            if selected is None:
                emit_json(
                    {
                        "ok": False,
                        "error": "ambiguous_or_missing_chat",
                        "query": args.query,
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
                "chat": {
                    "id": selected.get("id") or selected.get("chat_id"),
                    "dialog_id": dialog_id,
                    "title": selected.get("title") or selected.get("name"),
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
