# IS4B RAG — Поисковая система по проектной документации

Гибридная RAG-система (семантика + BM25/TF-IDF) для поиска по документации КИСУ «Метро» и ИС «Галактика ERP».

## Состав

```text
is4brag/
├── is4brag/                 # Переиспользуемый Python-пакет ingestion
├── fixtures/                # Golden queries для оценки поиска
├── schemas/                 # JSON Schema операционных fixtures
├── tests/                   # Быстрые тесты без сети и моделей
├── scripts/                  # Инфраструктура индексации
│   ├── sync_confluence.py    # Синхронизация Confluence → чанки
│   ├── resumable_index.py    # Возобновляемая индексация (с чекпоинтами)
│   ├── index_section.py      # Индексация одного раздела
│   ├── index_all.sh          # Индексация всех разделов
│   ├── ri_loop.sh            # Цикл индексации (песочница)
│   ├── ri_loop_host.sh       # Цикл индексации (хост)
│   └── setup_cron.sh         # Настройка cron (ежедневная синхронизация)
└── skills/                   # Навыки AI-ассистента
    ├── kisu-metro/           # КИСУ «Метро» (5 разделов, ~19k чанков)
    │   ├── SKILL.md
    │   └── scripts/
    │       └── kisu_metro_search.py
    └── galaktika-erp/        # Галактика ERP + КИСУ Метро
        ├── SKILL.md
        └── scripts/
            ├── galaktika_search.py
            ├── spec_search.py
            └── project_search.py
```

## База знаний

### КИСУ «Метро» (18 964 чанка)
5 разделов проектной документации из Confluence `conf-metro.ibs.ru`:
- **Стадии проекта** (6 331) — проектные решения, опросные листы, BPMN
- **Архитектура** (3 375) — целевая архитектура, контуры, интеграции
- **Управление проектом** (7 054) — протоколы советов, реестры
- **Спецификации требований** (2 153) — требования к подсистемам
- **Термины и сокращения** (51) — глоссарий

Модель: `intfloat/multilingual-e5-large` (1024d), chunk_size=800, overlap=150.

### Галактика ERP (16 835 чанков)
Справочная документация ИС «Галактика ERP»: хозоперации, МТО, контроллинг, кадры, зарплата.

Модель: `paraphrase-multilingual-MiniLM-L12-v2` (384d).

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/Balastov/is4brag.git
cd is4brag

# 2. Настроить токен Confluence
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e .
cp .env.example .env
# Отредактировать .env: вставить CONFLUENCE_PAT

# 3. Синхронизировать данные из Confluence
python3 scripts/sync_confluence.py --force

# 4. Построить индексы
python3 scripts/index_all.sh

# 5. Поиск
python3 skills/kisu-metro/scripts/kisu_metro_search.py "матрица соответствия" --top-k 5 --json
python3 skills/galaktika-erp/scripts/galaktika_search.py "хозоперации" --top-k 5 --json
```

Минимальная установка для запуска синхронизации: `pip install -e .`.
Группа `env` добавляет загрузку `.env`, `index` — локальные модели/TF-IDF,
`qdrant` — необязательный клиент Qdrant, `dev` — pytest/ruff.
Для воспроизводимой установки на сервере используется `requirements.lock`;
после него проект устанавливается командой `pip install -e .`.

## Конфигурация и надёжность ingestion

Настройки читаются централизованно из окружения (`is4brag.config.Settings`).
Полный список с безопасными примерами находится в `.env.example`: пути host/sandbox,
Confluence URL/PAT, версии модели/chunker/schema, Qdrant и SQLite.

`sync_confluence.py` сохраняет прежние CLI-флаги и формат
`<раздел>/chunks_export.jsonl`, но новые записи имеют стабильные content-addressed
`chunk_id`, `content_hash`, `chunker_version` и `schema_version`. Content-aware
chunker сохраняет heading path, целые строки таблиц с повторённым header,
metadata/ссылки требований и выбирается через `IS4BRAG_CHUNK_STRATEGY`.
Артефакты,
состояние и pending-файл заменяются атомарно; одновременный ingest блокируется
файлом `.sync_confluence.lock`. Старый глобальный `sync_state.json` автоматически
мигрирует в состояние по разделам, после чего каждый раздел checkpoint-ится отдельно.

Полная сверка состава страниц:

```bash
# один раздел или все разделы (без --section)
python scripts/sync_confluence.py --reconcile --section "Архитектура"
```

Она инвентаризирует все страницы, удаляет отсутствующие из corpus и создаёт
`tombstones.jsonl`; удалённые page_id также ставятся в очередь удаления.

### Canonical SQLite и Qdrant

По умолчанию sync одновременно сохраняет совместимый `chunks_export.jsonl` и
транзакционно обновляет `is4brag.sqlite3`. SQLite — источник истины для страниц,
чанков, FTS5 и очереди; Qdrant содержит только производный dense-индекс. Если
SQLite невозможно открыть при запуске, sync явно логирует legacy-режим и
использует прежний indexer. После успешного открытия canonical store любая
ошибка записи прерывает раздел и не продвигает watermark: частичный активный
store никогда не понижается до legacy. Явное управление: `--canonical-store`,
`--no-canonical-store` или `IS4BRAG_CANONICAL_STORE=0`.

```bash
# локальный Qdrant с persistent volume и localhost-only портами
docker compose -f docker-compose.qdrant.yml up -d

# worker: постоянно, один job или ограниченный batch
python -m is4brag.worker
python -m is4brag.worker --once
python -m is4brag.worker --max-jobs 100

# импорт существующих JSONL/совместимых embeddings.npy без Confluence
python scripts/import_legacy.py --base "/path/to/KISU Metro"

# SQLite/Qdrant drift и состояние очереди
python scripts/reconcile_store.py --base "/path/to/KISU Metro"
```

Worker использует lease/heartbeat, экспоненциальный retry и dead-letter после
исчерпания попыток. Эмбеддинги кэшируются по `content_hash + model_version`.
`sentence-transformers`, NumPy и `qdrant-client` импортируются только при
фактическом использовании; установка worker: `pip install -e ".[worker]"`.
Шаблоны systemd находятся в `systemd/`; `scripts/install_worker_service.sh`
только устанавливает unit и не запускает сервис автоматически.

### Search API

Общий search core объединяет Qdrant dense search и SQLite FTS5 с прежней
score-augmented RRF-семантикой 60/40, per-section рангами и безопасными exact
filters (`section`, `page_id`, `title`, `content_type`, `breadcrumbs`),
дедупликацией страниц и parent/document expansion. Провайдер кодирует запросы
через `embed_queries`; один прогретый экземпляр модели живёт весь срок процесса.

```bash
python -m pip install -e ".[api]"
python -m is4brag.api                    # SEARCH_API_BIND / SEARCH_API_PORT
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"матрица соответствия","top_k":5,"use_parents":true,
       "filters":{"content_type":"requirement"}}'
```

Endpoints: `/search`, `/webhooks/confluence`, `/health`, `/ready`, `/metrics`,
`/admin/reload`.
Readiness отдельно проверяет SQLite, Qdrant, прогретую модель и активный alias.
`/admin/reload` требует `SEARCH_ADMIN_TOKEN`; секреты не попадают в health и
логи. Параллелизм и deadline ограничиваются `SEARCH_API_CONCURRENCY` и
`SEARCH_API_TIMEOUT`.

Skill-клиент предпочитает API, когда задан `SEARCH_API_URL`, и по умолчанию
завершает поиск ошибкой при недоступности API, чтобы не вернуть удалённые или
устаревшие данные. Legacy fallback включается только явно:
`SEARCH_API_LEGACY_FALLBACK=1`. Без `SEARCH_API_URL` standalone file mode
продолжает работать как прежде.

Systemd-шаблоны: `systemd/is4brag-search-api.service` и
`systemd/search-api.env.example`. `scripts/install_search_api_service.sh`
устанавливает и enable-ит unit, но не запускает его.

### Webhook ingestion и eventual consistency

`POST /webhooks/confluence` проверяет HMAC-SHA256 из
`X-Hub-Signature-256`/`X-Hub-Signature: sha256=<hex>`, ограничивает размер/тип события и только
ставит минимальную запись в SQLite. Delivery ID берётся из
`X-Atlassian-Webhook-Delivery`, `X-Webhook-Delivery` или `X-Request-ID`;
повторная доставка безопасно дедуплицируется. Полный payload и секреты не
сохраняются и не логируются. Необязательный allowlist
`CONFLUENCE_WEBHOOK_ALLOWED_CIDRS` следует включать только если peer IP не
подменяется недоверенным proxy.

```bash
# drain одного события, bounded batch или постоянный worker
python scripts/ingest_event_worker.py --once
python scripts/ingest_event_worker.py --max-events 100
python -m is4brag.ingest

# authoritative inventory repair для удалений, перемещений и потерянных webhook
python scripts/sync_confluence.py --reconcile

# безопасно установить daily incremental + weekly reconcile; общий flock не даёт overlap
bash scripts/setup_cron.sh
```

Worker повторно загружает конкретную страницу из Confluence, определяет раздел
по ancestors, обновляет тот же canonical SQLite/chunk pipeline и совместимые
`chunks_export.jsonl`/`sync_state.json`. Delete/archive создаёт canonical
tombstone; неизвестное членство оставляет `reconcile_pending.json` как
операционный сигнал. Очередь использует lease, exponential backoff и dead-letter.
Webhook ускоряет обновления, но только периодический `--reconcile` является
авторитетным источником состава и гарантирует eventual consistency.
Systemd-шаблон и env: `systemd/is4brag-ingest-event-worker.service`,
`systemd/ingest-worker.env.example`; `scripts/install_ingest_worker_service.sh`
устанавливает и enable-ит unit, но не запускает его.

Метрики `/metrics`: `is4brag_webhook_*_total`,
`is4brag_ingest_queue_depth`, `is4brag_ingest_queue_oldest_age_seconds`,
`is4brag_ingest_processed_total`, `is4brag_ingest_dead_total`.

Shadow-сравнение legacy и API показывает overlap@k, изменения рангов и
golden-query quality gate:

```bash
python scripts/shadow_search.py --api-url http://127.0.0.1:8080 \
  --golden fixtures/golden_queries.json --top-k 10 --min-overlap .7 \
  --max-quality-degradation 0 --report reports/search-shadow.json
```

### E5 runtime, benchmark и безопасная смена модели

Контракт embedding разделяет документы и запросы: E5 всегда получает соответственно
`passage: ` и `query: `. PyTorch runtime настраивается через
`IS4BRAG_EMBEDDING_PROVIDER=pytorch`. ONNX runtime читает только локальные
`IS4BRAG_ONNX_MODEL_PATH` и `IS4BRAG_ONNX_TOKENIZER_PATH`; тесты ничего не скачивают.
Экспорт и необязательная INT8-квантизация выполняются отдельно:

```bash
python -m pip install -e ".[onnx,onnx-export]"
optimum-cli export onnx --model intfloat/multilingual-e5-large \
  --task feature-extraction artifacts/e5-large-onnx
optimum-cli onnxruntime quantize --avx2 \
  --onnx_model artifacts/e5-large-onnx -o artifacts/e5-large-int8
```

Каждый `IS4BRAG_MODEL_VERSION` индексируется в отдельную коллекцию
`QDRANT_COLLECTION__<version>`. Worker обслуживает только jobs своей версии.
Активный alias не переключается автоматически. Promotion выполняется только
через quality-gated experiment workflow ниже: он проверяет target SQLite,
settled queue, полный Qdrant manifest, metadata и dimensions. Утилита alias
разрешает только guarded rollback на явно указанные коллекции:

```bash
python scripts/requeue_model_version.py --model-version e5-large-onnx-int8-v1 \
  --runtime onnxruntime --dimensions 1024
IS4BRAG_MODEL_VERSION=e5-large-onnx-int8-v1 python -m is4brag.worker
# Значения берутся из сохранённого promotion report.
python scripts/manage_qdrant_alias.py rollback \
  --expected-current-collection kisu_metro__current-version-hash \
  --target-collection kisu_metro__previous-version-hash
```

Benchmark пишет JSON и возвращает exit code `2`, если абсолютный threshold или
допустимая деградация относительно baseline нарушены:

```bash
python scripts/benchmark_embeddings.py --provider pytorch --sample-size 500 \
  --golden fixtures/golden_queries.json --top-k 10 --min-recall .80 --min-mrr .70 \
  --report reports/pytorch.json
python scripts/benchmark_embeddings.py --provider onnx \
  --baseline-report reports/pytorch.json --max-degradation .02 \
  --report reports/onnx.json
```

Read-only анализ exact/near duplicates сообщает кандидатов и оценку экономии;
near duplicates никогда не удаляются:

```bash
python scripts/analyze_duplicates.py --report reports/duplicates.json
```

Re-chunk/model experiment сначала строится в отдельной SQLite/collection.
Alias может быть переключён только с явно прошедшим quality report:

```bash
python scripts/run_experiment.py --chunker-version 3 --target-model-version e5-v2
python scripts/run_experiment.py --apply --target-sqlite experiment.sqlite3 \
  --chunker-version 3 --target-model-version e5-v2
python scripts/run_experiment.py --target-sqlite experiment.sqlite3 \
  --chunker-version 3 --target-model-version e5-v2 \
  --quality-report reports/e5-v2.json --promote
```

Операционные метрики из локальных runtime-файлов:

```bash
python scripts/report_metrics.py --base "/path/to/KISU Metro"
```

Golden-query fixture находится в `fixtures/golden_queries.json`, его контракт —
в `schemas/golden_queries.schema.json`.

## Разработка

Тесты не используют сеть и не скачивают модели:

```bash
python -m pytest
# без установленного pytest:
python -m unittest discover -s tests -v
python -m py_compile is4brag/*.py scripts/*.py
```

## Автоматизация

### Автодеплой кода с GitHub → сервер (rag)

На `master@rag` один раз:

```bash
# если репо ещё нет локально — скрипт сам клонирует
curl -fsSL https://raw.githubusercontent.com/Balastov/is4brag/main/scripts/setup_autodeploy.sh -o /tmp/setup_autodeploy.sh
bash /tmp/setup_autodeploy.sh
```

Или из уже склонированного репо:

```bash
bash scripts/setup_autodeploy.sh
```

По умолчанию на rag:
- `KISU_BASE=/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro`
- `SKILLS_PUBLIC=/home/master/deer-flow/skills/public` (→ `/app/skills/public` в `deer-flow-gateway`)

Дальше каждые 5 минут cron делает `git fetch` и при новых коммитах в `main` копирует скрипты и skills.  
Лог: `~/is4brag/deploy.log`.

Ручной деплой:

```bash
cd ~/is4brag && git pull && bash scripts/deploy.sh
```

### Ежедневная синхронизация Confluence в 21:00

```bash
bash scripts/setup_cron.sh
```

Cron тянет только чанки (`--skip-index`). Индекс — отдельно на хосте:

```bash
cd "/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro"
.venv/bin/python sync_confluence.py          # pending + инкремент по page_id
# полная пересборка «Стадий» (редко):
# .venv/bin/python sync_confluence.py --full-reindex --section "Стадии проекта"
# или: nohup ./ri_loop_host.sh "Стадии проекта" &
```

## Требования

- Python 3.9+
- `sentence-transformers`, `numpy`, `scikit-learn`
- Confluence Data Center 8.5+ (Personal Access Token)
