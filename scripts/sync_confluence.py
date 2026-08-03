#!/usr/bin/env python3
r"""
Инкрементальная синхронизация Confluence → локальные чанки + индексы.
Запускать на хосте:
  python3 sync_confluence.py --base "/путь/до/KISU Metro"
Или через cron:
  0 21 * * * cd /путь/до/KISU\ Metro && .venv/bin/python sync_confluence.py >> sync.log 2>&1

Индексация:
  - обычный дифф → index_section.py батчами по PAGE_BATCH_SIZE page_id;
  - «Стадии проекта» и дифф ≥ LARGE_DIFF_PAGES → полная resumable_index.py;
  - сбой индексации → reindex_pending.json, повтор на следующем запуске.
"""
import json, os, sys, time, argparse, logging, hashlib, shutil, subprocess
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Конфигурация ──────────────────────────────────────────
CONFLUENCE_URL = "https://conf-metro.ibs.ru"
PAT = os.getenv("CONFLUENCE_PAT", "")

# Разделы для синхронизации: {название: page_id}
SECTIONS = {
    "Термины и сокращения": "1933357",
    "Управление проектом": "1933362",
    "Стадии проекта": "1933363",
    "Архитектура": "2820058",
    "KISU Metro - Спецификации требований": "1933456",
}

CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
MIN_CHUNK_LEN = 100
WORKERS = 5
TIMEOUT = 30

# Индексация: батчи не меняют итоговый индекс (тот же keep/replace + полный BM25),
# только снижают риск timeout на одном огромном --pages.
PAGE_BATCH_SIZE = 75
PAGE_BATCH_TIMEOUT = 3600          # сек на один батч инкремента
LARGE_DIFF_PAGES = 200             # порог «большого» диффа
RESUMABLE_SECTIONS = {"Стадии проекта"}
RESUMABLE_LOOP_TIMEOUT = 43200     # 12 ч на полный resumable-цикл
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
    import re
    if not raw:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.I | re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.I | re.DOTALL)

    table_idx = 0

    def _replace_table(match):
        nonlocal table_idx
        table_idx += 1
        converted = _table_to_text(match.group(0), table_idx)
        return "\n" + converted + "\n" if converted else "\n"

    text = re.sub(r'<table[^>]*>.*?</table>', _replace_table, text, flags=re.I | re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p\s*>', '\n', text, flags=re.I)
    text = re.sub(r'</h[1-6]\s*>', '\n', text, flags=re.I)
    text = re.sub(r'</tr\s*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = _unescape_html(text)
    # Сжимаем пробелы внутри строк, пустые строки — одна
    lines = []
    for line in text.splitlines():
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip()

# Обратная совместимость для внешних вызовов/тестов
def clean_html(raw: str) -> str:
    """Раньше склеивал всё в одну строку; теперь сохраняет структуру (таблицы/абзацы)."""
    return html_to_text(raw)

def page_breadcrumbs(page: dict) -> str:
    """Путь ancestors + title страницы: «Раздел > … > ПР_…»."""
    parts = []
    for anc in page.get("ancestors") or []:
        t = (anc.get("title") or "").strip()
        if t:
            parts.append(t)
    title = (page.get("title") or "").strip()
    if title and (not parts or parts[-1] != title):
        parts.append(title)
    return " > ".join(parts)

def chunk_text(text: str) -> list[str]:
    """
    Чанкирование с предпочтением границ строк (строки таблиц не режутся посередине,
    если строка короче CHUNK_SIZE).
    """
    if not text or not text.strip():
        return []

    lines = text.split("\n")
    chunks = []
    buf: list[str] = []
    buf_len = 0

    def _flush(overlap: bool):
        nonlocal buf, buf_len
        chunk = "\n".join(buf).strip()
        if len(chunk) >= MIN_CHUNK_LEN:
            chunks.append(chunk)
        if not overlap or not buf:
            buf, buf_len = [], 0
            return
        # overlap по хвосту ~CHUNK_OVERLAP символов
        kept: list[str] = []
        ol = 0
        for line in reversed(buf):
            add = len(line) + (1 if kept else 0)
            if kept and ol + add > CHUNK_OVERLAP:
                break
            kept.insert(0, line)
            ol += add
        buf, buf_len = kept, sum(len(x) + 1 for x in kept)

    for line in lines:
        # Очень длинная строка — режем по символам
        if len(line) > CHUNK_SIZE:
            if buf:
                _flush(overlap=True)
            start = 0
            while start < len(line):
                piece = line[start:start + CHUNK_SIZE].strip()
                if len(piece) >= MIN_CHUNK_LEN:
                    chunks.append(piece)
                start += CHUNK_SIZE - CHUNK_OVERLAP
            continue

        add_len = len(line) + (1 if buf else 0)
        if buf and buf_len + add_len > CHUNK_SIZE:
            _flush(overlap=True)
        buf.append(line)
        buf_len += add_len

    if buf:
        _flush(overlap=False)
    return chunks

def page_to_chunks(page: dict) -> list[dict]:
    """Конвертирует страницу Confluence в чанки (таблицы + breadcrumbs)."""
    body = page.get("body", {}).get("storage", {}).get("value", "")
    title = page.get("title", "Без названия")
    page_id = page.get("id", "")
    url = f"{CONFLUENCE_URL}/spaces/METRO/pages/{page_id}"
    breadcrumbs = page_breadcrumbs(page)
    body_text = html_to_text(body)
    header = f"Путь: {breadcrumbs}\nЗаголовок: {title}\n\n" if breadcrumbs else f"Заголовок: {title}\n\n"

    chunks = []
    for i, chunk in enumerate(chunk_text(body_text)):
        chunk_id = hashlib.md5(f"{page_id}_{i}".encode()).hexdigest()[:12]
        chunks.append({
            "chunk_id": chunk_id,
            "page_id": page_id,
            "title": title,
            "url": url,
            "breadcrumbs": breadcrumbs,
            "text": header + chunk,
            "chunk_index": i,
        })
    # Страница без тела, но с заголовком — один короткий чанк-заглушка не создаём
    # (MIN_CHUNK_LEN отфильтрует). Если только title важен — он в breadcrumbs соседних.
    return chunks

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

    # ── Fallback: если потомков нет, забираем саму страницу ──
    if not pages:
        page = api_get(session, f"content/{section_id}",
                       {"expand": "body.storage,version,ancestors,space"})
        if page:
            # Проверяем дату модификации только при инкрементальной синхронизации
            if since:
                modified = page.get("version", {}).get("when", "")
                if modified and _parse_date(modified) < _parse_date(since):
                    return []  # страница не менялась — пропускаем
            pages = [page]

    return pages

# ── Сохранение состояния ──────────────────────────────────
def load_state(base: str) -> dict:
    path = os.path.join(base, "sync_state.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"last_sync": None, "page_versions": {}}

def save_state(base: str, state: dict):
    # Формат: «2026-07-19T14:49:49+00:00» — совместим с datetime.fromisoformat()
    state["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(os.path.join(base, "sync_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)

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

def reindex_section(logger, base: str, section: str, changed_page_ids: list) -> bool:
    """Выбирает стратегию индексации и запускает её."""
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
        section in RESUMABLE_SECTIONS
        and len(changed_page_ids) >= LARGE_DIFF_PAGES
    )
    if use_resumable:
        logger.info(f"  Дифф {len(changed_page_ids)} ≥ {LARGE_DIFF_PAGES} "
                    f"для «{section}» → полная resumable-индексация "
                    f"(точность = полный пересчёт по актуальных чанкам)")
        ok = run_resumable_reindex(logger, base, section, python_bin)
    else:
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
                 state: dict, force: bool = False, skip_index: bool = False):
    logger.info(f"{'='*50}")
    logger.info(f"Синхронизация: {section} (ID={section_id})")

    out_dir = os.path.join(base, section)
    os.makedirs(out_dir, exist_ok=True)
    chunks_path = os.path.join(out_dir, "chunks_export.jsonl")

    # 1. Проверяем изменения
    since = None if force else state.get("last_sync")
    pages = get_changed_pages(session, section_id, since)
    logger.info(f"  Изменённых страниц: {len(pages)}")

    pending = load_pending_reindex(base)
    pending_ids = list(pending.get(section, []))

    if not pages and not force and not pending_ids:
        logger.info(f"  Нет изменений — пропускаем")
        return

    # 2. Загружаем существующие чанки (сгруппированные по page_id)
    existing = {}  # {page_id: [chunk_dicts]}
    if os.path.exists(chunks_path):
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                pid = c["page_id"]
                if pid not in existing:
                    existing[pid] = []
                existing[pid].append(c)

    # 3. Обновляем чанки для изменённых страниц
    updated_count = 0
    skipped_count = 0
    changed_page_ids = []  # page_id, для которых реально обновлены чанки

    for page in pages:
        pid = page["id"]
        new_version = page.get("version", {}).get("number", 0)
        old_version = state.get("page_versions", {}).get(pid, 0)

        if new_version <= old_version and not force:
            skipped_count += 1
            continue

        chunks = page_to_chunks(page)
        existing[pid] = chunks  # заменяем все чанки страницы целиком
        updated_count += len(chunks)
        changed_page_ids.append(pid)

        state["page_versions"][pid] = new_version

    # 4. Перезаписываем chunks_export.jsonl (только если были обновления чанков)
    all_chunks = []
    for chunks_list in existing.values():
        all_chunks.extend(chunks_list)

    if updated_count > 0:
        with open(chunks_path, "w", encoding="utf-8") as f:
            for c in all_chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        logger.info(f"  Всего чанков: {len(all_chunks)} "
                    f"(добавлено: {updated_count}, без изменений: {skipped_count})")
    elif pending_ids:
        logger.info(f"  Чанки без изменений Confluence; pending reindex: {len(pending_ids)} стр.")

    # 5. Перестраиваем индекс (новые изменения + незавершённые с прошлых запусков)
    reindex_ids = list(dict.fromkeys(changed_page_ids + pending_ids))
    if not reindex_ids or not os.path.exists(chunks_path):
        return

    if skip_index:
        # Только обновить чанки; индекс — позже (pending подхватит следующий sync без --skip-index)
        mark_reindex_pending(base, section, reindex_ids)
        logger.info(f"  ⏸  --skip-index: индексация отложена "
                    f"({len(reindex_ids)} стр. → reindex_pending.json)")
        return

    extra = f", pending={len(pending_ids)}" if pending_ids else ""
    logger.info(f"  Перестроение индекса ({len(reindex_ids)} стр.{extra})...")
    try:
        reindex_section(logger, base, section, reindex_ids)
    except Exception as e:
        logger.error(f"  ❌ Ошибка запуска индексации: {e}")
        mark_reindex_pending(base, section, reindex_ids)

def main():
    parser = argparse.ArgumentParser(description="Инкрементальная синхронизация Confluence")
    parser.add_argument("--base", default=os.path.dirname(os.path.abspath(__file__)),
                        help="Путь к папке KISU Metro")
    parser.add_argument("--force", action="store_true",
                        help="Полная пересинхронизация (игнорировать версии)")
    parser.add_argument("--skip-index", action="store_true",
                        help="Только чанки из Confluence, без переиндексации "
                             "(страницы пишутся в reindex_pending.json)")
    parser.add_argument("--section", help="Синхронизировать только указанный раздел")
    args = parser.parse_args()

    logger = setup_logging(args.base)
    state = load_state(args.base)

    logger.info("=" * 60)
    mode = "FULL" if args.force else "INCREMENTAL"
    if args.skip_index:
        mode += "+SKIP_INDEX"
    logger.info(f"🚀 Синхронизация Confluence | Режим: {mode}")
    logger.info(f"   URL: {CONFLUENCE_URL}")
    logger.info(f"   BASE: {args.base}")
    logger.info(f"   PID: {os.getpid()}")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {PAT}"})

    sections_to_sync = SECTIONS
    if args.section:
        if args.section not in SECTIONS:
            logger.error(f"Неизвестный раздел: {args.section}")
            sys.exit(1)
        sections_to_sync = {args.section: SECTIONS[args.section]}

    for section, sid in sections_to_sync.items():
        try:
            sync_section(session, logger, args.base, section, sid, state,
                         force=args.force, skip_index=args.skip_index)
        except Exception as e:
            logger.error(f"❌ Ошибка в разделе '{section}': {e}", exc_info=True)

    save_state(args.base, state)
    logger.info("=" * 60)
    logger.info("✅ Синхронизация завершена")
    if args.skip_index:
        logger.info("   Индексация отложена (--skip-index). "
                    "Следующий sync без флага подхватит reindex_pending.json")

if __name__ == "__main__":
    main()
