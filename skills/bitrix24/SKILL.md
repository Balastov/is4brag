---
name: bitrix24
description: |
  Use this skill when a user asks about Bitrix24 project work:

  - group / project chat discussions (what was discussed, decisions, open questions)
  - task time tracking (how much time was logged, by whom)
  - task chat history tied to a Bitrix task

  Triggers: «Битрикс», «Bitrix», «Битрикс24», «B24», названия чатов вроде «УДО 01»,
  «Провести анализ и оптимизацию backend», «что обсуждали в чате»,
  «сколько времени списано», «списания», «трудозатраты», «elapsed», «задача …».
---

# Bitrix24 — чаты и трудозатраты

Live-доступ к облачному Битрикс24 через входящий webhook. Данные **не** лежат в RAG:
скрипты ходят в REST API и возвращают JSON; модель суммирует / отвечает пользователю.

## Важно: доступ ≠ «без пользователя»

Входящий webhook **всегда** работает от имени пользователя, который его создал.
Bitrix24 **не отдаёт** историю чатов «в никуда» без субъекта прав.

Если Руслан (текущий владелец webhook) **не состоит** в чате/группе — API вернёт
пусто / access denied, даже если он ADMIN на портале.

Рабочая схема «системного» доступа:

1. Завести служебного пользователя (например `deerflow-bot@…`) или выбрать ТУЗ.
2. Добавить его **участником** во все нужные группы/чаты (можно без уведомлений).
3. Под этим пользователем создать входящий webhook со scope ниже.
4. `BITRIX24_WEBHOOK_URL` в DeerFlow — только URL этого webhook.

Админ + scope `sonet` помогает **найти** группу (`sg…`); чтение сообщений всё равно
требует членства в диалоге.

Примеры целевых чатов:

- `УДО 01🙆☀📑` (поиск по подстроке `УДО 01` ок — эмодзи в матче игнорируются)
- `Провести анализ и оптимизацию backend`

## Переменные окружения

Обязательно на хосте / в gateway:

```bash
BITRIX24_WEBHOOK_URL=https://<portal>.bitrix24.ru/rest/<user_id>/<code>/
```

Альтернатива:

```bash
BITRIX24_PORTAL=https://<portal>.bitrix24.ru
BITRIX24_USER_ID=<user_id>
BITRIX24_WEBHOOK_CODE=<code>
```

### Права входящего webhook (scopes)

При создании webhook в Б24 включи минимум:

| Право | Зачем |
|---|---|
| **Чат и Уведомления (im)** | поиск чатов, чтение сообщений |
| **Задачи (task / tasks)** | поиск задач, списания времени |
| **Пользователи (user / user_brief)** | ФИО авторов / исполнителей |
| **Соцсеть / Рабочие группы (sonet / socialnetwork)** | **обязательно** для проектных чатов `sg…` («УДО 01») |

Без `sonet`/`socialnetwork` webhook вернёт `insufficient_scope` на `sonet_group.get`,
и чаты проектов по названию не находятся (обычный `im.search` их не видит).

После смены прав **пересоздай входящий webhook** (старый URL не подхватывает новые scope)
и обнови `BITRIX24_WEBHOOK_URL`.

Поиск чата: `im.search.chat.list` → `sonet_group.get` → `socialnetwork.api.workgroup.list`
→ `im.recent.list`. В ответе поле `tried` (счётчики / ERROR).

## Скрипты

Пути в DeerFlow обычно:

```bash
python3 "/app/skills/public/bitrix24/scripts/chat_summary.py" ...
python3 "/app/skills/public/bitrix24/scripts/task_time.py" ...
```

Локально из репозитория:

```bash
python3 skills/bitrix24/scripts/chat_summary.py "УДО 01" --days 14 --json
python3 skills/bitrix24/scripts/chat_summary.py "Провести анализ и оптимизацию backend" --days 14 --json
python3 skills/bitrix24/scripts/task_time.py "Отработка сценариев тестирования 8 этап" --json
```

### 1) Сообщения чата → для саммари

```bash
python3 "/app/skills/public/bitrix24/scripts/chat_summary.py" "УДО 01" --days 14 --limit 200 --json
python3 "/app/skills/public/bitrix24/scripts/chat_summary.py" "Провести анализ и оптимизацию backend" --days 14 --json
```

| Параметр | По умолчанию | Описание |
|---|---|---|
| query | обязателен* | Подстрока названия чата (эмодзи в названии не мешают) |
| `--chat-id` | — | `123` / `chat123` / `sg45` вместо поиска по имени |
| `--days N` | 14 | Окно истории |
| `--limit N` | 200 | Максимум сообщений |

\* либо `query`, либо `--chat-id`.

Успех:

```json
{
  "ok": true,
  "chat": {"id": "...", "dialog_id": "chat…", "title": "УДО 01…"},
  "period": {"days": 14, "from": "...", "to": "..."},
  "message_count": 42,
  "messages": [{"id": 1, "date": "...", "author": "…", "text": "…"}],
  "instruction": "Summarize substantial discussion…"
}
```

Если чатов несколько — `ok: false`, `error: ambiguous_or_missing_chat`, список `candidates`.
Спроси уточнение или повтори с `--chat-id`.

### 2) Списания по задаче

```bash
python3 "/app/skills/public/bitrix24/scripts/task_time.py" "Отработка сценариев тестирования 8 этап" --json
```

| Параметр | Описание |
|---|---|
| title | Название задачи (точное или подстрока) |
| `--task-id` | Числовой ID вместо поиска |

Успех:

```json
{
  "ok": true,
  "task": {"id": "…", "title": "…", "status": "…", "responsible": "…"},
  "total_seconds": 36000,
  "total_hours": 10.0,
  "by_user": [{"user": "…", "hours": 4.5, "seconds": 16200, "entries": 3}],
  "entries": [{"user": "…", "hours": 1.0, "comment": "…", "created": "…"}],
  "entry_count": 5
}
```

При неоднозначном названии — `ambiguous_or_missing_task` + `candidates`.

## Алгоритм ответа

1. Определи тип вопроса: **чат** или **трудозатраты**.
2. Вызови соответствующий скрипт с `--json`.
3. Если `ambiguous_*` — покажи кандидатов и попроси ID / уточнение.
4. Для чата: по `messages` сформулируй существенное (решения, вопросы, риски, action items), без воды; указывай авторов и даты.
5. Для задачи: назови `total_hours`, при необходимости разбивку `by_user` и комментарии из `entries`.
6. Не выдумывай часы или цитаты вне JSON.

## Smoke-проверка webhook

Скрипты живут в **git-клоне** (`~/is4brag`) или после `deploy.sh` в
`SKILLS_PUBLIC` (обычно `/home/master/deer-flow/skills/public/bitrix24/`),
**не** в каталоге `KISU Metro`.

```bash
export BITRIX24_WEBHOOK_URL='https://….bitrix24.ru/rest/…/…/'

# Вариант A: из клона репозитория
cd ~/is4brag/skills/bitrix24/scripts
python3 -c "from bitrix_client import call; print(call('profile'))"
python3 probe.py --find "УДО 01"
python3 chat_summary.py "УДО 01" --days 3 --limit 20 --json
python3 chat_summary.py "Провести анализ и оптимизацию backend" --days 3 --limit 20 --json

# Вариант B: после deploy.sh
cd /home/master/deer-flow/skills/public/bitrix24/scripts
python3 chat_summary.py "УДО 01" --days 3 --limit 20 --json
```

## Ограничения

- Только то, что видит **владелец webhook** (нужен сервисный пользователь в целевых чатах).
- История чата читается страницами (до ~50 сообщений за запрос); очень длинные треды режь `--days` / `--limit`.
- Это live API, не индекс Confluence: свежо, но без офлайн-поиска по всем чатам сразу.
