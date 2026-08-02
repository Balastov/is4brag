# IS4B RAG — Поисковая система по проектной документации

Гибридная RAG-система (семантика + BM25/TF-IDF) для поиска по документации КИСУ «Метро» и ИС «Галактика ERP».

## Состав

```text
is4brag/
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
# при необходимости:
# SKILLS_PUBLIC=/path/to/skills/public bash scripts/setup_autodeploy.sh
```

Дальше каждые 5 минут cron делает `git fetch` и при новых коммитах в `main` копирует скрипты в  
`/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro` и skills в DeerFlow `skills/public`.  
Лог: `~/is4brag/deploy.log`.

Ручной деплой:

```bash
cd ~/is4brag && git pull && bash scripts/deploy.sh
```

### Ежедневная синхронизация Confluence в 21:00

```bash
bash scripts/setup_cron.sh
```

## Требования

- Python 3.10+
- `sentence-transformers`, `numpy`, `scikit-learn`
- Confluence Data Center 8.5+ (Personal Access Token)
