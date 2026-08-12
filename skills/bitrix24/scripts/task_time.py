#!/usr/bin/env python3
"""Aggregate Bitrix24 elapsed time for a task by title or id.

Usage:
  python3 task_time.py "Отработка сценариев тестирования 8 этап" [--json]
  python3 task_time.py --task-id 12345 [--json]
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from bitrix_client import (  # noqa: E402
    BitrixError,
    call,
    emit_error,
    emit_json,
    format_hours,
    resolve_users,
)


def find_tasks(title: str, limit: int = 20) -> list[dict[str, Any]]:
    # Prefer substring match via %query%
    needle = title.strip()
    result = call(
        "tasks.task.list",
        {
            "filter": {"%TITLE": needle},
            "select": [
                "ID",
                "TITLE",
                "STATUS",
                "GROUP_ID",
                "RESPONSIBLE_ID",
                "CREATED_BY",
                "TIME_SPENT_IN_LOGS",
                "DURATION_FACT",
            ],
            "order": {"ID": "DESC"},
            "start": 0,
        },
    )
    tasks: list[dict[str, Any]] = []
    if isinstance(result, dict):
        tasks = result.get("tasks") or result.get("list") or []
    elif isinstance(result, list):
        tasks = result

    # Normalize: Bitrix often nests under "task" or returns flat fields
    normalized = []
    for item in tasks:
        task = item.get("task") if isinstance(item, dict) and "task" in item else item
        if isinstance(task, dict):
            normalized.append(task)
    # API may ignore start/limit; trim client-side
    return normalized[:limit]


def get_task(task_id: str | int) -> dict[str, Any]:
    result = call(
        "tasks.task.get",
        {
            "taskId": int(task_id),
            "select": [
                "ID",
                "TITLE",
                "STATUS",
                "GROUP_ID",
                "RESPONSIBLE_ID",
                "CREATED_BY",
                "TIME_SPENT_IN_LOGS",
                "DURATION_FACT",
            ],
        },
    )
    if isinstance(result, dict):
        if isinstance(result.get("task"), dict):
            return result["task"]
        return result
    raise BitrixError(f"Unexpected tasks.task.get payload: {result!r}")


def elapsed_list(task_id: str | int) -> list[dict[str, Any]]:
    # Legacy method signature: ORDER, FILTER, PARAMS, TASKID positionally-ish via form
    result = call(
        "task.elapseditem.getlist",
        {
            "TASKID": int(task_id),
            "ORDER": {"ID": "ASC"},
        },
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("items", "elapsedItems", "list"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def pick_task(tasks: list[dict[str, Any]], title: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not tasks:
        return None, []
    q = title.strip().casefold()
    exact = [t for t in tasks if str(t.get("title") or t.get("TITLE") or "").casefold() == q]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    if len(tasks) == 1:
        return tasks[0], []
    return None, tasks


def task_brief(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id") or task.get("ID"),
        "title": task.get("title") or task.get("TITLE"),
        "status": task.get("status") or task.get("STATUS"),
        "group_id": task.get("groupId") or task.get("GROUP_ID"),
        "responsible_id": task.get("responsibleId") or task.get("RESPONSIBLE_ID"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bitrix24 task elapsed time")
    parser.add_argument("title", nargs="?", help="Task title (exact or substring)")
    parser.add_argument("--task-id", help="Numeric task id")
    parser.add_argument("--json", action="store_true", help="JSON output (default)")
    args = parser.parse_args()

    if not args.title and not args.task_id:
        emit_error("Provide task title or --task-id")

    try:
        if args.task_id:
            task = get_task(args.task_id)
            candidates: list[dict[str, Any]] = []
        else:
            found = find_tasks(args.title or "")
            task, candidates = pick_task(found, args.title or "")
            if task is None:
                emit_json(
                    {
                        "ok": False,
                        "error": "ambiguous_or_missing_task",
                        "query": args.title,
                        "candidates": [task_brief(t) for t in candidates],
                    },
                    exit_code=2,
                )

        assert task is not None
        brief = task_brief(task)
        task_id = brief["id"]
        if task_id is None:
            emit_error("Task has no ID", extra={"task": task})

        entries_raw = elapsed_list(task_id)
        user_ids = [e.get("USER_ID") or e.get("userId") for e in entries_raw]
        responsible = brief.get("responsible_id")
        if responsible:
            user_ids.append(responsible)
        names = resolve_users([u for u in user_ids if u is not None])

        entries = []
        by_user: dict[str, dict[str, Any]] = {}
        total_seconds = 0
        for item in entries_raw:
            seconds = int(item.get("SECONDS") or item.get("seconds") or 0)
            uid = str(item.get("USER_ID") or item.get("userId") or "")
            total_seconds += seconds
            entries.append(
                {
                    "id": item.get("ID") or item.get("id"),
                    "user_id": uid,
                    "user": names.get(uid, uid),
                    "seconds": seconds,
                    "hours": format_hours(seconds),
                    "comment": item.get("COMMENT_TEXT") or item.get("commentText") or "",
                    "created": item.get("CREATED_DATE") or item.get("createdDate"),
                    "date_start": item.get("DATE_START") or item.get("dateStart"),
                    "date_stop": item.get("DATE_STOP") or item.get("dateStop"),
                }
            )
            bucket = by_user.setdefault(
                uid,
                {"user_id": uid, "user": names.get(uid, uid), "seconds": 0, "entries": 0},
            )
            bucket["seconds"] += seconds
            bucket["entries"] += 1

        by_user_list = []
        for bucket in by_user.values():
            bucket["hours"] = format_hours(bucket["seconds"])
            by_user_list.append(bucket)
        by_user_list.sort(key=lambda x: x["seconds"], reverse=True)

        # Fallback field from task itself if logs empty but fact filled
        fact = task.get("timeSpentInLogs") or task.get("TIME_SPENT_IN_LOGS")
        try:
            fact_seconds = int(fact) if fact not in (None, "") else None
        except (TypeError, ValueError):
            fact_seconds = None

        emit_json(
            {
                "ok": True,
                "query": args.title,
                "task": {
                    **brief,
                    "responsible": names.get(str(responsible), responsible) if responsible else None,
                },
                "total_seconds": total_seconds,
                "total_hours": format_hours(total_seconds),
                "task_time_spent_in_logs_seconds": fact_seconds,
                "by_user": by_user_list,
                "entries": entries,
                "entry_count": len(entries),
            }
        )
    except BitrixError as exc:
        emit_error(str(exc))


if __name__ == "__main__":
    main()
