"""Confluence HTML normalization, chunking, and stable chunk identities."""

from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import re
from typing import Dict, List, Optional, Tuple

from .config import Settings


@dataclass
class NormalizedDocument:
    text: str
    requirements: List[Dict[str, str]] = field(default_factory=list)


def _colspan_count(attributes: Dict[str, str]) -> int:
    raw = attributes.get("colspan") or "1"
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def _format_table_lines(
    table: List[Tuple[bool, List[str]]], table_number: int
) -> List[str]:
    """Serialize a table so each data row carries column names for retrieval."""
    header_cells: List[str] = []
    for is_header, cells in table:
        if is_header and any(cells):
            header_cells = list(cells)
            break
    lines = ["[Таблица %d]" % table_number]
    if header_cells:
        lines.append("| заголовок | %s |" % " | ".join(header_cells))
    data_index = 0
    emitted_primary_header = False
    for is_header, cells in table:
        if not any(cells):
            continue
        if is_header:
            if not emitted_primary_header and cells == header_cells:
                emitted_primary_header = True
                continue
            lines.append("| заголовок | %s |" % " | ".join(cells))
            continue
        data_index += 1
        if header_cells:
            labeled: List[str] = []
            for index, cell in enumerate(cells):
                name = header_cells[index] if index < len(header_cells) else ""
                if name:
                    labeled.append("%s: %s" % (name, cell))
                else:
                    labeled.append(cell)
            lines.append("| строка %d | %s |" % (data_index, " | ".join(labeled)))
        else:
            lines.append("| строка %d | %s |" % (data_index, " | ".join(cells)))
    return lines


class _ConfluenceParser(HTMLParser):
    BLOCKS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.tables: List[List[Tuple[bool, List[str]]]] = []
        self._table_stack: List[List[Tuple[bool, List[str]]]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._cell_colspan = 1
        self._header_cell = False
        self._in_thead = 0
        self._skip = 0
        self.requirements: List[Dict[str, str]] = []

    @property
    def _table(self) -> Optional[List[Tuple[bool, List[str]]]]:
        return self._table_stack[-1] if self._table_stack else None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        requirement = {
            key: value
            for key, value in attributes.items()
            if "requirement" in key
            or key in {
                "data-key",
                "data-status",
                "data-owner",
                "data-parent-ref",
                "data-child-ref",
            }
        }
        macro_name = attributes.get("ac:name", "")
        if requirement or "requirement" in macro_name.lower():
            if macro_name:
                requirement["macro"] = macro_name
            if requirement and requirement not in self.requirements:
                self.requirements.append(requirement)
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            # Nested tables: finish the open cell buffer first, then push a frame.
            if self._cell is not None:
                self._cell.append("\n")
            self._table_stack.append([])
            return
        if tag == "thead":
            self._in_thead += 1
            return
        if tag == "tr" and self._table is not None:
            self._row = []
            self._header_cell = self._in_thead > 0
            return
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_colspan = _colspan_count(attributes)
            self._header_cell = self._header_cell or tag == "th" or self._in_thead > 0
            return
        if tag == "br":
            self._append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self._append("\n" + ("#" * int(tag[1])) + " ")
        elif tag in self.BLOCKS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "thead":
            self._in_thead = max(0, self._in_thead - 1)
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = _clean_inline("".join(self._cell))
            for _ in range(self._cell_colspan):
                self._row.append(text)
            self._cell = None
            self._cell_colspan = 1
            return
        if tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append((self._header_cell, self._row))
            self._row = None
            return
        if tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            self.tables.append(table)
            lines = _format_table_lines(table, len(self.tables))
            rendered = "\n" + "\n".join(lines) + "\n"
            if self._table_stack and self._cell is not None:
                # Nested table stays inside the parent cell text.
                self._cell.append(rendered)
            else:
                self.parts.append(rendered)
            return
        if tag in self.BLOCKS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._append(data)

    def _append(self, value: str) -> None:
        if self._cell is not None:
            self._cell.append(value)
        elif not self._table_stack:
            self.parts.append(value)


def _clean_inline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_html(raw: str) -> NormalizedDocument:
    if not raw:
        return NormalizedDocument("")
    parser = _ConfluenceParser()
    parser.feed(raw)
    parser.close()
    lines: List[str] = []
    for line in "".join(parser.parts).splitlines():
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
        elif lines and lines[-1] != "":
            lines.append("")
    return NormalizedDocument("\n".join(lines).strip(), parser.requirements)


def html_to_text(raw: str) -> str:
    return normalize_html(raw).text


def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 150,
    min_chunk_len: int = 100,
) -> List[str]:
    if not text or not text.strip():
        return []
    chunks: List[str] = []
    buffer: List[str] = []

    def length(lines: List[str]) -> int:
        return len("\n".join(lines))

    def flush(keep_overlap: bool) -> None:
        nonlocal buffer
        value = "\n".join(buffer).strip()
        if len(value) >= min_chunk_len:
            chunks.append(value)
        if not keep_overlap or overlap <= 0:
            buffer = []
            return
        kept: List[str] = []
        for line in reversed(buffer):
            candidate = [line] + kept
            if kept and length(candidate) > overlap:
                break
            kept = candidate
        buffer = kept

    for line in text.splitlines():
        if len(line) > chunk_size:
            if buffer:
                flush(True)
            step = max(1, chunk_size - overlap)
            for start in range(0, len(line), step):
                piece = line[start : start + chunk_size].strip()
                if len(piece) >= min_chunk_len:
                    chunks.append(piece)
            buffer = []
            continue
        if buffer and length(buffer + [line]) > chunk_size:
            flush(True)
        buffer.append(line)
    if buffer:
        flush(False)
    return chunks


def _table_blocks(lines: List[str]) -> List[Tuple[str, List[str]]]:
    """Split normalized text into prose/table blocks without splitting table rows."""
    blocks: List[Tuple[str, List[str]]] = []
    prose: List[str] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("[Таблица "):
            if prose:
                blocks.append(("prose", prose))
                prose = []
            table = [lines[index]]
            index += 1
            while index < len(lines) and lines[index].startswith("| "):
                table.append(lines[index])
                index += 1
            blocks.append(("table", table))
            continue
        prose.append(lines[index])
        index += 1
    if prose:
        blocks.append(("prose", prose))
    return blocks


def _prose_chunks(
    lines: List[str], chunk_size: int, overlap: int, min_chunk_len: int
) -> List[Tuple[str, dict]]:
    output: List[Tuple[str, dict]] = []
    heading_path: List[str] = []
    segment: List[str] = []

    def emit() -> None:
        nonlocal segment
        if not segment:
            return
        context = "\n".join(heading_path)
        body = "\n".join(segment).strip()
        value = (context + "\n" + body).strip() if context and not body.startswith(context) else body
        for piece in chunk_text(value, chunk_size, overlap, min_chunk_len):
            output.append((piece, {"content_type": "prose", "heading_path": list(heading_path)}))
        segment = []

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            emit()
            level = len(match.group(1))
            heading_path = heading_path[: level - 1] + [line]
        segment.append(line)
    emit()
    return output


def _table_chunks(
    lines: List[str], chunk_size: int, min_chunk_len: int
) -> List[Tuple[str, dict]]:
    if not lines:
        return []
    label = lines[0]
    rows = lines[1:]
    header = next((row for row in rows if row.startswith("| заголовок |")), "")
    data_rows = [row for row in rows if row != header]
    chunks: List[Tuple[str, dict]] = []
    current: List[str] = []
    for row in data_rows:
        prefix = [label] + ([header] if header else [])
        candidate = "\n".join(prefix + current + [row])
        if current and len(candidate) > chunk_size:
            text = "\n".join(prefix + current)
            chunks.append((text, {"content_type": "table", "table_header": header}))
            current = []
        # Oversized rows are intentionally kept whole.
        current.append(row)
    if current or header:
        text = "\n".join([label] + ([header] if header else []) + current)
        if len(text) >= min_chunk_len or rows:
            chunks.append((text, {"content_type": "table", "table_header": header}))
    return chunks


def chunk_document(
    document: NormalizedDocument,
    *,
    chunk_size: int,
    overlap: int,
    min_chunk_len: int,
    strategy: str = "auto",
) -> List[Tuple[str, dict]]:
    """Versioned content-aware chunking; ``legacy`` retains the old behavior."""
    if strategy == "legacy":
        return [
            (value, {"content_type": "prose", "heading_path": []})
            for value in chunk_text(document.text, chunk_size, overlap, min_chunk_len)
        ]
    if strategy not in {"auto", "content-aware"}:
        raise ValueError("unsupported chunk strategy: %s" % strategy)
    output: List[Tuple[str, dict]] = []
    requirement_tokens = {
        value.lower()
        for requirement in document.requirements
        for key, value in requirement.items()
        if value and key in {"data-key", "data-requirement-id", "requirement-id", "id"}
    }
    heading_path: List[str] = []
    for kind, lines in _table_blocks(document.text.splitlines()):
        if kind == "table":
            chunks = []
            for text, metadata in _table_chunks(lines, chunk_size, min_chunk_len):
                context = "\n".join(heading_path)
                chunks.append(
                    (
                        (context + "\n" + text).strip() if context else text,
                        {**metadata, "heading_path": list(heading_path)},
                    )
                )
        else:
            working_lines = list(lines)
            if heading_path and not any(
                re.match(r"^#{1,6}\s+", line) for line in working_lines[:1]
            ):
                working_lines = heading_path + working_lines
            chunks = _prose_chunks(
                working_lines, chunk_size, overlap, min_chunk_len
            )
            for line in lines:
                match = re.match(r"^(#{1,6})\s+(.+)$", line)
                if match:
                    level = len(match.group(1))
                    heading_path = heading_path[: level - 1] + [line]
        for text, metadata in chunks:
            lower = text.lower()
            matched = [
                dict(item)
                for item in document.requirements
                if not requirement_tokens
                or any(
                    value.lower() in lower
                    for value in item.values()
                    if isinstance(value, str)
                )
            ]
            if matched:
                metadata = {
                    **metadata,
                    "structural_content_type": metadata.get("content_type", "prose"),
                    "content_type": "requirement",
                    "requirement_metadata": matched,
                    "parent_references": sorted(
                        {
                            value
                            for item in matched
                            for key, value in item.items()
                            if "parent" in key and value
                        }
                    ),
                    "child_references": sorted(
                        {
                            value
                            for item in matched
                            for key, value in item.items()
                            if "child" in key and value
                        }
                    ),
                }
            output.append((text, metadata))
    return output


def page_breadcrumbs(page: dict) -> str:
    parts = [
        (ancestor.get("title") or "").strip()
        for ancestor in page.get("ancestors") or []
        if (ancestor.get("title") or "").strip()
    ]
    title = (page.get("title") or "").strip()
    if title and (not parts or parts[-1] != title):
        parts.append(title)
    return " > ".join(parts)


def content_hash(text: str) -> str:
    """Hash the exact UTF-8 text passed to the embedding provider."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_chunk_id(
    section: str,
    page_id: str,
    digest: str,
    duplicate_ordinal: int,
    strategy_version: str = "",
) -> str:
    fields = [section, page_id, digest, str(duplicate_ordinal)]
    if strategy_version:
        fields.append(strategy_version)
    identity = "\x1f".join(fields)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def page_to_chunks(page: dict, section: str, settings: Settings) -> List[dict]:
    body = page.get("body", {}).get("storage", {}).get("value", "")
    document = normalize_html(body)
    title = page.get("title", "Без названия")
    page_id = str(page.get("id", ""))
    breadcrumbs = page_breadcrumbs(page)
    header = (
        "Путь: %s\nЗаголовок: %s\n\n" % (breadcrumbs, title)
        if breadcrumbs
        else "Заголовок: %s\n\n" % title
    )
    ordinals: Dict[str, int] = {}
    records = []
    strategy = getattr(settings, "chunk_strategy", "auto")
    for index, (chunk, chunk_metadata) in enumerate(
        chunk_document(
            document,
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
            min_chunk_len=settings.min_chunk_len,
            strategy=strategy,
        )
    ):
        indexed_text = header + chunk
        digest = content_hash(indexed_text)
        ordinal = ordinals.get(digest, 0)
        ordinals[digest] = ordinal + 1
        records.append(
            {
                "chunk_id": stable_chunk_id(
                    section,
                    page_id,
                    digest,
                    ordinal,
                    "%s:%s" % (settings.chunker_version, strategy),
                ),
                "page_id": page_id,
                "section": section,
                "title": title,
                "url": "%s/spaces/METRO/pages/%s" % (settings.confluence_url, page_id),
                "breadcrumbs": breadcrumbs,
                "text": indexed_text,
                "chunk_index": index,
                "duplicate_ordinal": ordinal,
                "content_hash": digest,
                "chunker_version": settings.chunker_version,
                "schema_version": settings.schema_version,
                "requirements": document.requirements,
                "chunk_strategy": strategy,
                **chunk_metadata,
            }
        )
    return records
