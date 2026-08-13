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
    as_list,
    call,
    emit_error,
    emit_json,
    format_hours,
    resolve_users,
)

TASK_SELECT = [
    "ID",
    "TITLE",
    "STATUS",
    "GROUP_ID",
    "RESPONSIBLE_ID",
    "CREATED_BY",
    "TIME_SPENT_IN_LOGS",
    "DURATION_FACT",
]


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _task_title(task: dict[str, Any]) -> str:
    return str(task.get("title") or task.get("TITLE") or "").strip()


def _task_id_num(task: dict[str, Any]) -> int:
    raw = task.get("id") or task.get("ID") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _unwrap_tasks(result: Any) -> list[dict[str, Any]]:
    """Extract task objects from tasks.task.list payload.

    Important: empty list `[]` must stay empty — do not fall through to the
    wrapper dict (that produced a fake item with id='tasks').
    """
    if result is None:
        return []
    tasks: Any = result
    if isinstance(result, dict):
        if "tasks" in result:
            tasks = result["tasks"]
        elif "list" in result:
            tasks = result["list"]
        elif "items" in result:
            tasks = result["items"]
    normalized = []
    for item in as_list(tasks):
        if not isinstance(item, dict):
            continue
        task = item.get("task") if "task" in item and isinstance(item.get("task"), dict) else item
        # Skip as_list artifacts like {"id": "tasks", "value": ...}
        if "value" in task and not (_task_title(task) or _task_id_num(task)):
            continue
        if not (_task_title(task) or task.get("id") or task.get("ID")):
            continue
        normalized.append(task)
    return normalized


def find_tasks(title: str, limit: int = 20) -> tuple[list[dict[str, Any]], list[str]]:
    needle = title.strip()
    tried: list[str] = []
    filters = [
        ("TITLE %pattern%", {"TITLE": f"%{needle}%"}),
        ("%TITLE like", {"%TITLE": needle}),
        ("TITLE exact", {"TITLE": needle}),
    ]
    for label, filt in filters:
        tried.append(label)
        try:
            result = call(
                "tasks.task.list",
                {
                    "filter": filt,
                    "select": TASK_SELECT,
                    "order": {"ID": "desc"},
                    "start": 0,
                },
            )
        except BitrixError as exc:
            tried.append(f"{label} ERROR: {exc}")
            continue
        found = _unwrap_tasks(result)
        if found:
            scored = []
            q = _norm(needle)
            for task in found:
                t = _norm(_task_title(task))
                if t == q:
                    scored.append((0, task))
                elif q in t:
                    scored.append((1, task))
                else:
                    scored.append((2, task))
            scored.sort(key=lambda x: (x[0], -_task_id_num(x[1])))
            return [t for _, t in scored][:limit], tried
    return [], tried


def get_task(task_id: str | int) -> dict[str, Any]:
    result = call(
        "tasks.task.get",
        {
            "taskId": int(task_id),
            "select": TASK_SELECT,
        },
    )
    if isinstance(result, dict):
        if isinstance(result.get("task"), dict):
            return result["task"]
        return result
    raise BitrixError(f"Unexpected tasks.task.get payload: {result!r}")


def elapsed_list(task_id: str | int) -> list[dict[str, Any]]:
    result = call(
        "task.elapseditem.getlist",
        {
            "TASKID": int(task_id),
            "ORDER": {"ID": "ASC"},
        },
    )
    if isinstance(result, dict):
        for key in ("items", "elapsedItems", "list"):
            if result.get(key) is not None:
                return as_list(result.get(key))
    return as_list(result)


def pick_task(tasks: list[dict[str, Any]], title: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not tasks:
        return None, []
    q = _norm(title)
    exact = [t for t in tasks if _norm(_task_title(t)) == q]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    contains = [t for t in tasks if q in _norm(_task_title(t))]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, contains
    if len(tasks) == 1:
        return tasks[0], []
    return None, tasks


def task_brief(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id") or task.get("ID"),
        "title": _task_title(task) or None,
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
        tried: list[str] = []
        if args.task_id:
            task = get_task(args.task_id)
            candidates: list[dict[str, Any]] = []
        else:
            found, tried = find_tasks(args.title or "")
            task, candidates = pick_task(found, args.title or "")
            if task is None:
                emit_json(
                    {
                        "ok": False,
                        "error": "ambiguous_or_missing_task",
                        "query": args.title,
                        "tried": tried,
                        "hint": (
                            "Webhook user must have task scope and access to the task. "
                            "Retry with --task-id if the title is not unique."
                        ),
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

        fact = task.get("timeSpentInLogs") or task.get("TIME_SPENT_IN_LOGS")
        try:
            fact_seconds = int(fact) if fact not in (None, "") else None
        except (TypeError, ValueError):
            fact_seconds = None

        emit_json(
            {
                "ok": True,
                "query": args.title,
                "tried": tried,
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
