#!/usr/bin/env python3
r"""
Инкрементальная синхронизация Confluence → локальные чанки + индексы.
Запускать на хосте:
  python3 sync_confluence.py --base "/путь/до/KISU Metro"
Или через cron:
  0 0 * * * cd /путь/до/KISU\ Metro && python3 sync_confluence.py >> sync.log 2>&1
"""
import json, os, sys, time, argparse, logging, hashlib
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

def clean_html(raw: str) -> str:
    """Простая очистка HTML → plain text"""
    import re
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-z]+;', lambda m: {'&amp;':'&','&lt;':'<','&gt;':'>',
                    '&quot;':'"','&nbsp;':' ','&apos;':"'"}.get(m.group(), ' '), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def chunk_text(text: str) -> list[str]:
    """Простое чанкирование с перекрытием"""
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start:start + CHUNK_SIZE]
        if len(chunk.strip()) >= MIN_CHUNK_LEN:
            chunks.append(chunk.strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks

def page_to_chunks(page: dict) -> list[dict]:
    """Конвертирует страницу Confluence в чанки"""
    body = page.get("body", {}).get("storage", {}).get("value", "")
    title = page.get("title", "Без названия")
    page_id = page.get("id", "")
    url = f"{CONFLUENCE_URL}/spaces/METRO/pages/{page_id}"
    text = clean_html(body)
    chunks = []
    for i, chunk in enumerate(chunk_text(text)):
        chunk_id = hashlib.md5(f"{page_id}_{i}".encode()).hexdigest()[:12]
        chunks.append({
            "chunk_id": chunk_id,
            "page_id": page_id,
            "title": title,
            "url": url,
            "text": chunk,
            "chunk_index": i,
        })
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

# ── Главный цикл ──────────────────────────────────────────
def sync_section(session, logger, base: str, section: str, section_id: str,
                 state: dict, force: bool = False):
    logger.info(f"{'='*50}")
    logger.info(f"Синхронизация: {section} (ID={section_id})")

    out_dir = os.path.join(base, section)
    os.makedirs(out_dir, exist_ok=True)
    chunks_path = os.path.join(out_dir, "chunks_export.jsonl")

    # 1. Проверяем изменения
    since = None if force else state.get("last_sync")
    pages = get_changed_pages(session, section_id, since)
    logger.info(f"  Изменённых страниц: {len(pages)}")

    if not pages and not force:
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
    new_count = 0
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

    # 4. Перезаписываем chunks_export.jsonl
    all_chunks = []
    for chunks_list in existing.values():
        all_chunks.extend(chunks_list)
    with open(chunks_path, "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    logger.info(f"  Всего чанков: {len(all_chunks)} "
                f"(добавлено: {updated_count}, без изменений: {skipped_count})")

    # 5. Перестраиваем индекс (если были изменения)
    if updated_count > 0 and len(all_chunks) > 0:
        logger.info(f"  Перестроение индекса...")
        try:
            import subprocess
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_section.py")
            # Авто-определение: .venv на хосте, python3 в контейнере
            venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
            python_bin = venv_python if os.path.exists(venv_python) else "python3"

            # Pre-check: numpy должен быть доступен (иначе падает внутри index_section.py)
            check = subprocess.run(
                [python_bin, "-c", "import numpy"],
                capture_output=True, text=True, timeout=10
            )
            if check.returncode != 0:
                logger.warning(f"  ⚠️  numpy не найден — пропускаем индексацию "
                               f"(установи: {python_bin} -m pip install numpy)")
            else:
                # Инкрементальная индексация (только изменённые страницы)
                cmd = [python_bin, script, section,
                       "--pages", ",".join(changed_page_ids)]
                logger.info(f"  Индексация: {' '.join(cmd[3:])}")
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=14400
                )
                if result.returncode == 0:
                    logger.info(f"  ✅ Индекс обновлён")
                else:
                    logger.error(f"  ❌ Ошибка индексации: {result.stderr[:500]}")
        except Exception as e:
            logger.error(f"  ❌ Ошибка запуска индексации: {e}")

def main():
    parser = argparse.ArgumentParser(description="Инкрементальная синхронизация Confluence")
    parser.add_argument("--base", default=os.path.dirname(os.path.abspath(__file__)),
                        help="Путь к папке KISU Metro")
    parser.add_argument("--force", action="store_true",
                        help="Полная пересинхронизация (игнорировать версии)")
    parser.add_argument("--section", help="Синхронизировать только указанный раздел")
    args = parser.parse_args()

    logger = setup_logging(args.base)
    state = load_state(args.base)

    logger.info("=" * 60)
    logger.info(f"🚀 Синхронизация Confluence | Режим: {'FULL' if args.force else 'INCREMENTAL'}")
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
            sync_section(session, logger, args.base, section, sid, state, args.force)
        except Exception as e:
            logger.error(f"❌ Ошибка в разделе '{section}': {e}", exc_info=True)

    save_state(args.base, state)
    logger.info("=" * 60)
    logger.info("✅ Синхронизация завершена")

if __name__ == "__main__":
    main()
