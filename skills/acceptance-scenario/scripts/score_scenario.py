#!/usr/bin/env python3
"""Score a filled acceptance scenario markdown for page_id coverage.

Counts steps in sections 6 and 7. Does not decide true hallucinations —
those need a human label. Prints JSON plus a short table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional


PAGE_ID_RE = re.compile(
    r"(?:page[_ ]?id\s*[=:]\s*)?(\d{6,})|(?:pages/(?:viewpage\.action\?pageId=)?)(\d{6,})",
    re.I,
)
GAP_RE = re.compile(r"НЕ НАЙДЕНО", re.I)
HEADING_RE = re.compile(r"^#{1,6}\s+")
STEP_HEADING_RE = re.compile(
    r"шаги сценария|негативн|граничн",
    re.I,
)


def _cells(line: str) -> Optional[List[str]]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    parts = [part.strip() for part in stripped.strip("|").split("|")]
    if not parts:
        return None
    if all(re.fullmatch(r":?-{3,}:?", part.replace(" ", "")) for part in parts):
        return None
    return parts


def _page_ids(text: str) -> List[str]:
    found: List[str] = []
    for match in PAGE_ID_RE.finditer(text or ""):
        value = match.group(1) or match.group(2)
        if value and value not in found:
            found.append(value)
    return found


def _is_header_row(cells: List[str]) -> bool:
    first = cells[0].lower().replace("№", "#")
    if first in {"#", "n"}:
        labels = " ".join(cell.lower() for cell in cells[1:3])
        return "действие" in labels or "предусловие" in labels
    joined = " ".join(cells).lower()
    return joined.startswith("действие") or (
        "действие" in joined and "источник" in joined and not any(
            re.fullmatch(r"\d+", cell) for cell in cells[:1]
        )
    )


def _row_empty(cells: List[str]) -> bool:
    body = cells[1:] if cells and re.fullmatch(r"\d+", cells[0]) else cells
    return not any(cell and cell not in {"-", "—"} for cell in body)


def extract_steps(markdown: str) -> List[dict]:
    steps: List[dict] = []
    in_step_table = False
    section = ""
    for raw in markdown.splitlines():
        if HEADING_RE.match(raw):
            title = HEADING_RE.sub("", raw).strip()
            in_step_table = bool(STEP_HEADING_RE.search(title))
            if in_step_table:
                section = title
            continue
        if not in_step_table:
            continue
        cells = _cells(raw)
        if cells is None:
            if raw.strip() == "":
                continue
            if HEADING_RE.match(raw):
                in_step_table = False
            continue
        if _is_header_row(cells) or _row_empty(cells):
            continue
        source = cells[-1] if cells else ""
        action = cells[1] if len(cells) > 1 else ""
        expected = cells[2] if len(cells) > 2 else ""
        page_ids = _page_ids(" ".join(cells))
        gap = bool(GAP_RE.search(" ".join(cells)))
        steps.append(
            {
                "section": section,
                "action": action,
                "expected": expected,
                "source": source,
                "page_ids": page_ids,
                "explicit_gap": gap,
                "ungrounded_candidate": bool(
                    (action or expected) and not page_ids and not gap
                ),
            }
        )
    return steps


def score(markdown: str) -> dict:
    steps = extract_steps(markdown)
    total = len(steps)
    with_page_id = sum(1 for step in steps if step["page_ids"])
    gaps = sum(1 for step in steps if step["explicit_gap"])
    ungrounded = sum(1 for step in steps if step["ungrounded_candidate"])
    pct = round(100.0 * with_page_id / total, 1) if total else 0.0
    return {
        "total_steps": total,
        "with_page_id": with_page_id,
        "with_page_id_pct": pct,
        "explicit_gaps": gaps,
        "ungrounded_candidates": ungrounded,
        "unique_page_ids": sorted(
            {page_id for step in steps for page_id in step["page_ids"]}
        ),
        "steps": steps,
        "note": "Hallucinated_steps must be labelled by a human; ungrounded_candidates are only suspects.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=Path, help="Filled scenario .md from DeerFlow")
    parser.add_argument("--pilot-id", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    text = args.markdown.read_text(encoding="utf-8")
    result = score(text)
    result["pilot_id"] = args.pilot_id or args.markdown.stem
    result["path"] = str(args.markdown)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "%s  steps=%d  with_page_id=%d (%.1f%%)  gaps=%d  ungrounded_candidates=%d  page_ids=%s"
            % (
                result["pilot_id"],
                result["total_steps"],
                result["with_page_id"],
                result["with_page_id_pct"],
                result["explicit_gaps"],
                result["ungrounded_candidates"],
                ",".join(result["unique_page_ids"]) or "-",
            )
        )
        for index, step in enumerate(result["steps"], 1):
            flag = (
                "GAP"
                if step["explicit_gap"]
                else ("OK" if step["page_ids"] else "UNGROUNDED")
            )
            print(
                "  %2d [%s] %s | page_id=%s"
                % (
                    index,
                    flag,
                    (step["action"] or "")[:80],
                    ",".join(step["page_ids"]) or "-",
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
