#!/usr/bin/env python3
r"""
Инкрементальная синхронизация Confluence → локальные чанки + индексы.
Запускать на хосте:
  python3 sync_confluence.py --base "/путь/до/KISU Metro"
Или через cron:
  0 21 * * * cd /путь/до/KISU\ Metro && .venv/bin/python sync_confluence.py >> sync.log 2>&1

Индексация:
  - по умолчанию → index_section.py батчами по PAGE_BATCH_SIZE page_id
    (в т.ч. «Стадии проекта» — инкремент, без полной пересборки);
  - --full-reindex → для RESUMABLE_SECTIONS при большом диффе: resumable_index.py;
  - сбой индексации → reindex_pending.json, повтор на следующем sync без --skip-index.
  Cron обычно: sync_confluence.py --skip-index (только чанки).
"""
import json, os, sys, time, argparse, logging, shutil, subprocess
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import requests
except ImportError:  # Keep normalization/state helpers importable in minimal environments.
    requests = None
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from is4brag.config import DEFAULT_SECTIONS, Settings
from is4brag.content import (
    chunk_text as _chunk_text,
    html_to_text as _html_to_text,
    normalize_html as _normalize_html,
    page_breadcrumbs as _page_breadcrumbs,
    page_to_chunks as _page_to_chunks,
)
from is4brag.io import FileLock, atomic_write_json, atomic_write_jsonl
from is4brag.reconcile import append_tombstones, make_tombstones, reconcile_chunks
from is4brag.store import CanonicalStore
from is4brag.state import (
    load_state as _load_state,
    save_state as _save_state,
    section_state,
)

if load_dotenv:
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

SETTINGS = Settings.from_env()

# ── Конфигурация ──────────────────────────────────────────
CONFLUENCE_URL = SETTINGS.confluence_url
PAT = SETTINGS.confluence_pat

# Разделы для синхронизации: {название: page_id}
SECTIONS = dict(DEFAULT_SECTIONS)

CHUNK_SIZE = SETTINGS.chunk_size
CHUNK_OVERLAP = SETTINGS.chunk_overlap
MIN_CHUNK_LEN = SETTINGS.min_chunk_len
WORKERS = SETTINGS.workers
TIMEOUT = SETTINGS.request_timeout

# Индексация: батчи не меняют итоговый индекс (тот же keep/replace + полный BM25),
# только снижают риск timeout на одном огромном --pages.
PAGE_BATCH_SIZE = 75
PAGE_BATCH_TIMEOUT = 3600          # сек на один батч инкремента
LARGE_DIFF_PAGES = 200             # порог для --full-reindex → resumable
RESUMABLE_SECTIONS = {"Стадии проекта"}
RESUMABLE_LOOP_TIMEOUT = 43200     # 12 ч на полный resumable-цикл (--full-reindex)
PENDING_REINDEX_FILE = "reindex_pending.json"

# ── Утилиты ───────────────────────────────────────────────
def setup_logging(base: str):
    log_path = os.path.join(base, "sync.log")
    logger = logging.getLogger("sync")
    logger.setLevel(logging.INFO)
    # Предотвращаем дублирование handler'ов при повторных вызовах
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

def api_get(session, endpoint, params=None):
    url = f"{CONFLUENCE_URL}/rest/api/{endpoint}"
    resp = session.get(url, params=params, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {PAT}"})
    resp.raise_for_status()
    return resp.json()

_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&nbsp;": " ", "&apos;": "'", "&#39;": "'",
}

def _unescape_html(text: str) -> str:
    import re
    text = re.sub(
        r'&[a-zA-Z]+;|&#\d+;|&#x[0-9a-fA-F]+;',
        lambda m: _HTML_ENTITIES.get(m.group(), m.group()),
        text,
    )
    return text

def _strip_tags(html: str) -> str:
    import re
    text = re.sub(r'<[^>]+>', ' ', html)
    text = _unescape_html(text)
    return re.sub(r'[ \t]+', ' ', text).strip()

def _table_to_text(table_html: str, table_idx: int) -> str:
    """Confluence <table> → текстовые строки с номерами (для поиска по строкам БП)."""
    import re
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, flags=re.I | re.DOTALL)
    if not rows:
        return _strip_tags(table_html)
    lines = [f"[Таблица {table_idx}]"]
    for ri, row in enumerate(rows, 1):
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, flags=re.I | re.DOTALL)
        cells = [_strip_tags(c) for c in cells]
        if not any(cells):
            continue
        is_header = bool(re.search(r'<th\b', row, flags=re.I))
        label = "заголовок" if is_header else f"строка {ri}"
        lines.append("| " + label + " | " + " | ".join(cells) + " |")
    return "\n".join(lines) if len(lines) > 1 else ""

def html_to_text(raw: str) -> str:
    """
    HTML storage Confluence → plain text.
    Таблицы сохраняются построчно с метками «строка N», остальное — с переносами.
    """
    return _html_to_text(raw)

# Обратная совместимость для внешних вызовов/тестов
def clean_html(raw: str) -> str:
    """Раньше склеивал всё в одну строку; теперь сохраняет структуру (таблицы/абзацы)."""
    return html_to_text(raw)

def page_breadcrumbs(page: dict) -> str:
    """Путь ancestors + title страницы: «Раздел > … > ПР_…»."""
    return _page_breadcrumbs(page)

def chunk_text(text: str) -> list[str]:
    """
    Чанкирование с предпочтением границ строк (строки таблиц не режутся посередине,
    если строка короче CHUNK_SIZE).
    """
    return _chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP, MIN_CHUNK_LEN)

def page_to_chunks(page: dict, section: str = "") -> list[dict]:
    """Конвертирует страницу Confluence в чанки (таблицы + breadcrumbs)."""
    return _page_to_chunks(page, section, SETTINGS)

# ── Проверка изменений ────────────────────────────────────
def _format_cql_date(iso_str: str) -> str:
    """«2026-07-19T14:49:49+00:00» → «2026-07-19 14:49» (формат CQL)."""
    dt = _parse_date(iso_str)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M")
    # fallback — грубое обрезание (если fromisoformat не справился)
    return iso_str[:16].replace("T", " ")


def _parse_date(date_str: str):
    """Парсит дату в любом разумном ISO-формате (с/без микросекунд и timezone)."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        pass
    # Confluence-формат: «2026-07-19T14:49:49.000+0300» → нет двоеточия в tz
    try:
        s = date_str.replace("+0000", "+00:00").replace("-0000", "-00:00")
        # обрабатываем «+0300» → «+03:00»
        if len(s) >= 5 and s[-5] in "+-" and s[-4:].isdigit() and ":" not in s[-5:]:
            s = s[:-2] + ":" + s[-2:]
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def get_changed_pages(session, section_id: str, since: str = None) -> list[dict]:
    """
    CQL: все страницы-потомки, изменённые с даты since.
    Если потомков нет (напр. «Термины и сокращения»), возвращает саму страницу.
    """
    cql = f"ancestor={section_id}"
    if since:
        # CQL принимает только «yyyy-MM-dd HH:mm» — обрезаем микросекунды и timezone
        cql_date = _format_cql_date(since)
        cql += f" AND lastModified >= '{cql_date}'"
    pages = []
    start = 0
    limit = 50
    while True:
        data = api_get(session, "content/search", {
            "cql": cql, "start": start, "limit": limit,
            "expand": "body.storage,version,ancestors,space"
        })
        results = data.get("results", [])
        pages.extend(results)
        if len(results) < limit:
            break
        start += limit

    # The section root is not included by ancestor=... and must be checked
    # independently even when descendants also changed.
    root = api_get(session, f"content/{section_id}",
                   {"expand": "body.storage,version,ancestors,space"})
    if root:
        include_root = True
        if since:
            modified = _parse_date(root.get("version", {}).get("when", ""))
            watermark = _parse_date(since)
            include_root = modified is None or watermark is None or modified >= watermark
        if include_root:
            pages.append(root)

    return list({str(page.get("id", "")): page for page in pages}.values())


def get_section_inventory(session, section_id: str) -> list[dict]:
    """Return every descendant plus the section root, de-duplicated by page ID."""
    pages = get_changed_pages(session, section_id, None)
    by_id = {str(page.get("id", "")): page for page in pages}
    return list(by_id.values())

# ── Сохранение состояния ──────────────────────────────────
def load_state(base: str) -> dict:
    return _load_state(base, SECTIONS)

def save_state(
    base: str,
    state: dict,
    completed_section: str = None,
    checkpoint: str = None,
):
    _save_state(base, state, completed_section, checkpoint)

def _pending_path(base: str) -> str:
    return os.path.join(base, PENDING_REINDEX_FILE)

def load_pending_reindex(base: str) -> dict:
    path = _pending_path(base)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_pending_reindex(base: str, pending: dict):
    path = _pending_path(base)
    if not pending:
        if os.path.exists(path):
            os.remove(path)
        return
    atomic_write_json(Path(path), pending)

def mark_reindex_pending(base: str, section: str, page_ids: list):
    pending = load_pending_reindex(base)
    old = set(pending.get(section, []))
    pending[section] = sorted(old | set(page_ids))
    save_pending_reindex(base, pending)

def clear_reindex_pending(base: str, section: str):
    pending = load_pending_reindex(base)
    if section in pending:
        pending.pop(section, None)
        save_pending_reindex(base, pending)


def clear_processed_pending(base: str, section: str, page_ids):
    pending = load_pending_reindex(base)
    remaining = set(pending.get(section, [])) - set(page_ids)
    if remaining:
        pending[section] = sorted(remaining)
    else:
        pending.pop(section, None)
    save_pending_reindex(base, pending)

def _python_bin(script_dir: str) -> str:
    venv_python = os.path.join(script_dir, ".venv", "bin", "python")
    return venv_python if os.path.exists(venv_python) else "python3"

def run_resumable_reindex(logger, base: str, section: str, python_bin: str) -> bool:
    """Полная возобновляемая индексация раздела (для больших диффов «Стадии»)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    indexer = os.path.join(script_dir, "resumable_index.py")
    if not os.path.exists(indexer):
        logger.error(f"  ❌ Не найден resumable_index.py: {indexer}")
        return False

    # Чистый старт: старый чекпоинт мог быть от другого числа чанков
    ckpt = os.path.join(base, section, ".checkpoint")
    if os.path.isdir(ckpt):
        shutil.rmtree(ckpt)
        logger.info("  Удалён старый .checkpoint перед полной индексацией")

    env = os.environ.copy()
    env["KISU_METRO_BASE"] = base
    env.setdefault("RESUMABLE_MAX_RUNTIME", "14400")  # хост: 4 ч на процесс
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env.setdefault(k, "4")

    logger.info(f"  Resumable полная индексация: {section} "
                f"(лимит цикла {RESUMABLE_LOOP_TIMEOUT}с)")
    t0 = time.time()
    run = 0
    while True:
        if time.time() - t0 > RESUMABLE_LOOP_TIMEOUT:
            logger.error(f"  ❌ Resumable: превышен лимит {RESUMABLE_LOOP_TIMEOUT}с")
            return False
        run += 1
        logger.info(f"  Resumable запуск #{run}...")
        result = subprocess.run(
            [python_bin, indexer, section],
            capture_output=True, text=True, env=env
        )
        if result.stdout:
            logger.info(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        if result.returncode == 0:
            logger.info(f"  ✅ Resumable индекс готов ({run} запусков)")
            return True
        if result.returncode == 2:
            logger.info("  → чекпоинт сохранён, продолжаем...")
            continue
        err = (result.stderr or result.stdout or "")[:800]
        logger.error(f"  ❌ Resumable ошибка (exit={result.returncode}): {err}")
        return False

def run_batched_incremental_reindex(logger, base: str, section: str,
                                    page_ids: list, python_bin: str) -> bool:
    """
    Инкрементальная индексация батчами page_id.
    Итог эквивалентен одному --pages со всеми id: для каждого батча
    удаляются старые эмбеддинги страниц и добавляются новые, BM25
    пересобирается по полному актуальному корпусу.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    index_script = os.path.join(script_dir, "index_section.py")
    if not os.path.exists(index_script):
        logger.error(f"  ❌ Не найден index_section.py: {index_script}")
        return False

    total = len(page_ids)
    n_batches = (total + PAGE_BATCH_SIZE - 1) // PAGE_BATCH_SIZE
    logger.info(f"  Инкрементальная индексация батчами: {total} стр. "
                f"× batch={PAGE_BATCH_SIZE} ({n_batches} батч.)")

    for i in range(0, total, PAGE_BATCH_SIZE):
        batch = page_ids[i:i + PAGE_BATCH_SIZE]
        batch_no = i // PAGE_BATCH_SIZE + 1
        logger.info(f"  Батч {batch_no}/{n_batches}: {len(batch)} стр.")
        cmd = [python_bin, index_script, section, "--pages", ",".join(batch)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=PAGE_BATCH_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.error(f"  ❌ Батч {batch_no}/{n_batches}: timeout "
                         f"после {PAGE_BATCH_TIMEOUT}с")
            # Помечаем оставшиеся (включая текущий) для повтора
            mark_reindex_pending(base, section, page_ids[i:])
            return False
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "")[:500]
            logger.error(f"  ❌ Батч {batch_no}/{n_batches}: {err}")
            mark_reindex_pending(base, section, page_ids[i:])
            return False

    logger.info("  ✅ Индекс обновлён (все батчи)")
    return True

def reindex_section(logger, base: str, section: str, changed_page_ids: list,
                    full_reindex: bool = False) -> bool:
    """Выбирает стратегию индексации и запускает её.

    По умолчанию — инкремент по page_id (в т.ч. «Стадии проекта»).
    Полный resumable только при full_reindex и большом диффе в RESUMABLE_SECTIONS.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    python_bin = _python_bin(script_dir)

    check = subprocess.run(
        [python_bin, "-c", "import numpy"],
        capture_output=True, text=True, timeout=10
    )
    if check.returncode != 0:
        logger.warning(f"  ⚠️  numpy не найден — пропускаем индексацию "
                       f"(установи: {python_bin} -m pip install numpy)")
        mark_reindex_pending(base, section, changed_page_ids)
        return False

    use_resumable = (
        full_reindex
        and section in RESUMABLE_SECTIONS
        and len(changed_page_ids) >= LARGE_DIFF_PAGES
    )
    if use_resumable:
        logger.info(f"  --full-reindex: дифф {len(changed_page_ids)} ≥ {LARGE_DIFF_PAGES} "
                    f"для «{section}» → полная resumable-индексация")
        ok = run_resumable_reindex(logger, base, section, python_bin)
    else:
        if section in RESUMABLE_SECTIONS and len(changed_page_ids) >= LARGE_DIFF_PAGES:
            logger.info(f"  Дифф {len(changed_page_ids)} стр. для «{section}» — "
                        f"инкремент (полный resumable только с --full-reindex)")
        ok = run_batched_incremental_reindex(
            logger, base, section, changed_page_ids, python_bin
        )

    if ok:
        clear_reindex_pending(base, section)
    else:
        mark_reindex_pending(base, section, changed_page_ids)
    return ok

# ── Главный цикл ──────────────────────────────────────────
def sync_section(session, logger, base: str, section: str, section_id: str,
                 state: dict, force: bool = False, skip_index: bool = False,
                 full_reindex: bool = False, reconcile: bool = False,
                 canonical_store=None, model_version: str = None):
    logger.info(f"{'='*50}")
    logger.info(f"Синхронизация: {section} (ID={section_id})")

    out_dir = os.path.join(base, section)
    os.makedirs(out_dir, exist_ok=True)
    chunks_path = os.path.join(out_dir, "chunks_export.jsonl")

    current_state = section_state(state, section)
    # Reconciliation deliberately inventories the entire section. Incremental runs
    # keep the existing CQL query and therefore preserve the old CLI's cost profile.
    since = None if (force or reconcile) else current_state.get("last_sync")
    fetched_pages = (
        get_section_inventory(session, section_id)
        if reconcile
        else get_changed_pages(session, section_id, since)
    )
    inventory_ids = {str(page["id"]) for page in fetched_pages} if reconcile else set()
    pages = fetched_pages
    if reconcile and not force:
        versions = current_state.get("page_versions", {})
        pages = [
            page for page in fetched_pages
            if page.get("version", {}).get("number", 0) > versions.get(str(page["id"]), 0)
        ]
    logger.info(f"  Изменённых страниц: {len(pages)}")

    pending = load_pending_reindex(base)
    pending_ids = list(pending.get(section, []))

    if not pages and not force and not pending_ids and not reconcile:
        logger.info(f"  Нет изменений — пропускаем")
        return True

    # 2. Загружаем существующие чанки (сгруппированные по page_id)
    existing = {}  # {page_id: [chunk_dicts]}
    if os.path.exists(chunks_path):
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                c = json.loads(line)
                pid = c["page_id"]
                if pid not in existing:
                    existing[pid] = []
                existing[pid].append(c)

    # 3. Обновляем чанки для изменённых страниц
    updated_count = 0
    skipped_count = 0
    changed_page_ids = []  # page_id, для которых реально обновлены чанки
    changed_pages = {}

    for page in pages:
        pid = str(page["id"])
        new_version = page.get("version", {}).get("number", 0)
        old_version = current_state.get("page_versions", {}).get(pid, 0)

        if new_version <= old_version and not force:
            skipped_count += 1
            continue

        chunks = page_to_chunks(page, section)
        existing[pid] = chunks  # заменяем все чанки страницы целиком
        changed_pages[pid] = page
        updated_count += len(chunks)
        changed_page_ids.append(pid)

        current_state["page_versions"][pid] = new_version

    stale_page_ids = set()
    if reconcile:
        flattened = [chunk for page_chunks in existing.values() for chunk in page_chunks]
        known_page_ids = (
            set(map(str, current_state.get("inventory", [])))
            | set(map(str, current_state.get("page_versions", {}).keys()))
        )
        if current_state.get("ownership_known", False):
            _, stale_page_ids = reconcile_chunks(flattened, inventory_ids, known_page_ids)
        else:
            logger.info("  Первый authoritative inventory: удаления отложены до следующей сверки")
        for pid in stale_page_ids:
            existing.pop(pid, None)
            current_state["page_versions"].pop(pid, None)
        current_state["inventory"] = sorted(inventory_ids)
        current_state["ownership_known"] = True
        tombstones = make_tombstones(section, stale_page_ids)
        append_tombstones(Path(base) / "tombstones.jsonl", tombstones)
        if stale_page_ids:
            logger.info(f"  Удалено отсутствующих страниц: {len(stale_page_ids)}")

    # 4. Перезаписываем chunks_export.jsonl (только если были обновления чанков)
    all_chunks = []
    for chunks_list in existing.values():
        all_chunks.extend(chunks_list)

    if changed_page_ids or stale_page_ids:
        atomic_write_jsonl(Path(chunks_path), all_chunks)
        logger.info(f"  Всего чанков: {len(all_chunks)} "
                    f"(добавлено: {updated_count}, без изменений: {skipped_count})")
    elif pending_ids:
        logger.info(f"  Чанки без изменений Confluence; pending reindex: {len(pending_ids)} стр.")

    # SQLite is canonical when configured, while JSONL remains a compatibility export.
    canonical_queue = canonical_store is not None
    if canonical_queue:
        # Once an active canonical store has accepted any work, failure must abort
        # the section so its watermark cannot advance past a partial SQLite write.
        queued_ids: list[str] = []
        for pid in changed_page_ids:
            page = changed_pages[pid]
            page_chunks = existing.get(pid, [])
            canonical_store.replace_page(
                {
                    "page_id": pid,
                    "section": section,
                    "title": page.get("title", ""),
                    "url": (
                        f"{CONFLUENCE_URL}/spaces/METRO/pages/{pid}"
                    ),
                    "breadcrumbs": page_breadcrumbs(page),
                    "confluence_version": page.get("version", {}).get("number", 0),
                    "schema_version": SETTINGS.schema_version,
                    "source": page,
                    "source_text": _normalize_html(
                        page.get("body", {}).get("storage", {}).get("value", "")
                    ).text,
                    "parent_text": _normalize_html(
                        page.get("body", {}).get("storage", {}).get("value", "")
                    ).text,
                },
                page_chunks,
                model_version or SETTINGS.model_version,
            )
            queued_ids.append(pid)
        # Drain legacy reindex_pending into SQLite from current JSONL chunks so
        # nights spent with CANONICAL_STORE=0 do not leave the API index stale.
        pending_queued = 0
        for pid in pending_ids:
            if pid in queued_ids or pid in stale_page_ids:
                continue
            page_chunks = existing.get(pid, [])
            if not page_chunks:
                continue
            first = page_chunks[0]
            canonical_store.replace_page(
                {
                    "page_id": pid,
                    "section": section,
                    "title": first.get("title", ""),
                    "url": first.get(
                        "url",
                        f"{CONFLUENCE_URL}/spaces/METRO/pages/{pid}",
                    ),
                    "breadcrumbs": first.get("breadcrumbs", ""),
                    "confluence_version": int(
                        current_state.get("page_versions", {}).get(pid, 0) or 0
                    ),
                    "schema_version": first.get(
                        "schema_version", SETTINGS.schema_version
                    ),
                    "source": {"legacy_pending": True},
                    "source_text": first.get("text", ""),
                    "parent_text": first.get("text", ""),
                },
                page_chunks,
                model_version or SETTINGS.model_version,
            )
            queued_ids.append(pid)
            pending_queued += 1
        for pid in stale_page_ids:
            canonical_store.tombstone_page(
                pid, model_version or SETTINGS.model_version
            )
        skipped_pending = [
            pid for pid in pending_ids
            if pid not in queued_ids and pid not in stale_page_ids
        ]
        if skipped_pending:
            logger.warning(
                "  Pending без чанков в JSONL, оставлено в reindex_pending: %d стр."
                % len(skipped_pending)
            )
        if queued_ids or stale_page_ids:
            clear_processed_pending(
                base, section, list(dict.fromkeys(queued_ids + sorted(stale_page_ids)))
            )
            logger.info(
                "  SQLite queue: confluence=%d, pending=%d, удалено=%d"
                % (len(changed_page_ids), pending_queued, len(stale_page_ids))
            )

    # 5. Перестраиваем индекс (новые изменения + незавершённые с прошлых запусков)
    reindex_ids = list(dict.fromkeys(changed_page_ids + sorted(stale_page_ids) + pending_ids))
    if not reindex_ids or not os.path.exists(chunks_path):
        return True

    if canonical_queue:
        # The autonomous worker owns indexing; ingest must never launch a model process.
        logger.info(
            "  SQLite queue приняла %d page_id; indexer не запускается"
            % len(queued_ids)
        )
        return True

    if skip_index:
        # Только обновить чанки; индекс — позже (pending подхватит следующий sync без --skip-index)
        mark_reindex_pending(base, section, reindex_ids)
        logger.info(f"  ⏸  --skip-index: индексация отложена "
                    f"({len(reindex_ids)} стр. → reindex_pending.json)")
        return True

    extra = f", pending={len(pending_ids)}" if pending_ids else ""
    logger.info(f"  Перестроение индекса ({len(reindex_ids)} стр.{extra})...")
    try:
        reindex_section(logger, base, section, reindex_ids,
                        full_reindex=full_reindex)
    except Exception as e:
        logger.error(f"  ❌ Ошибка запуска индексации: {e}")
        mark_reindex_pending(base, section, reindex_ids)
    return True

def main():
    parser = argparse.ArgumentParser(description="Инкрементальная синхронизация Confluence")
    parser.add_argument("--base", default=os.path.dirname(os.path.abspath(__file__)),
                        help="Путь к папке KISU Metro")
    parser.add_argument("--force", action="store_true",
                        help="Полная пересинхронизация (игнорировать версии)")
    parser.add_argument("--skip-index", action="store_true",
                        help="Только чанки из Confluence, без переиндексации "
                             "(страницы пишутся в reindex_pending.json)")
    parser.add_argument("--full-reindex", action="store_true",
                        help="Для крупных диффов в RESUMABLE_SECTIONS — полная "
                             "resumable-индексация вместо инкремента по page_id")
    parser.add_argument("--section", help="Синхронизировать только указанный раздел")
    parser.add_argument("--reconcile", action="store_true",
                        help="Сверить полный состав страниц и удалить отсутствующие")
    canonical_group = parser.add_mutually_exclusive_group()
    canonical_group.add_argument(
        "--canonical-store", dest="canonical_store", action="store_true",
        help="Писать SQLite и ставить задания автономному index worker",
    )
    canonical_group.add_argument(
        "--no-canonical-store", dest="canonical_store", action="store_false",
        help="Отключить SQLite и сохранить прежний запуск index_section.py",
    )
    parser.set_defaults(canonical_store=None)
    args = parser.parse_args()

    if args.skip_index and args.full_reindex:
        parser.error("--skip-index и --full-reindex нельзя вместе")

    logger = setup_logging(args.base)
    state = load_state(args.base)
    env_canonical = os.getenv("IS4BRAG_CANONICAL_STORE", "1").lower() not in {
        "0", "false", "no", "off"
    }
    canonical_enabled = env_canonical if args.canonical_store is None else args.canonical_store
    canonical_store = None
    if canonical_enabled:
        try:
            settings = Settings.from_env(args.base)
            canonical_store = CanonicalStore(settings.sqlite_path)
        except Exception as exc:
            logger.error(f"⚠️ SQLite недоступен; используется legacy fallback: {exc}")

    logger.info("=" * 60)
    mode = "FULL" if args.force else "INCREMENTAL"
    if args.skip_index:
        mode += "+SKIP_INDEX"
    if args.full_reindex:
        mode += "+FULL_REINDEX"
    if args.reconcile:
        mode += "+RECONCILE"
    logger.info(f"🚀 Синхронизация Confluence | Режим: {mode}")
    logger.info(f"   URL: {CONFLUENCE_URL}")
    logger.info(f"   BASE: {args.base}")
    logger.info(f"   PID: {os.getpid()}")

    if requests is None:
        parser.error("requests не установлен; установите пакет: pip install -e .")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {PAT}"})

    sections_to_sync = SECTIONS
    if args.section:
        if args.section not in SECTIONS:
            logger.error(f"Неизвестный раздел: {args.section}")
            sys.exit(1)
        sections_to_sync = {args.section: SECTIONS[args.section]}

    failed_sections = []
    try:
        with FileLock(Path(args.base) / ".sync_confluence.lock", timeout=0):
            for section, sid in sections_to_sync.items():
                # Persist the pre-query watermark only after the section succeeds.
                # CQL minute precision supplies an overlap on the next run.
                query_watermark = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
                run_id = (
                    canonical_store.start_sync_run(section)
                    if canonical_store is not None
                    else None
                )
                try:
                    sync_section(session, logger, args.base, section, sid, state,
                                 force=args.force, skip_index=args.skip_index,
                                 full_reindex=args.full_reindex,
                                 reconcile=args.reconcile,
                                 canonical_store=canonical_store,
                                 model_version=SETTINGS.model_version)
                    # A crash in a later section cannot lose an earlier checkpoint.
                    save_state(
                        args.base,
                        state,
                        completed_section=section,
                        checkpoint=query_watermark,
                    )
                    if run_id is not None:
                        canonical_store.finish_sync_run(run_id, "completed")
                except Exception as e:
                    if run_id is not None:
                        canonical_store.finish_sync_run(run_id, "failed", error=str(e))
                    logger.error(f"❌ Ошибка в разделе '{section}': {e}", exc_info=True)
                    failed_sections.append(section)
    except TimeoutError as e:
        logger.error(f"❌ {e}")
        return 2
    finally:
        if canonical_store is not None:
            canonical_store.close()
    logger.info("=" * 60)
    if failed_sections:
        logger.error(
            "❌ Синхронизация завершилась с ошибками: %s",
            ", ".join(failed_sections),
        )
        return 1
    logger.info("✅ Синхронизация завершена")
    if args.skip_index:
        logger.info("   Индексация отложена (--skip-index). "
                    "Запустите sync без флага или index_section / ri_loop_host "
                    "для reindex_pending.json")
    return 0

if __name__ == "__main__":
    sys.exit(main())
