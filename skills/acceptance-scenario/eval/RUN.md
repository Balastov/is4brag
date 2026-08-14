# Как прогнать 3 пилота

После `git pull` + `bash scripts/deploy.sh` на `master@rag`.

## 1. Сбор источников (на хосте, контроль retrieval)

```bash
BASE="/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro"
SKILL="/home/master/deer-flow/skills/public/acceptance-scenario"
cd "$BASE"

.venv/bin/python "$SKILL/scripts/collect_sources.py" \
  --topic "UTR_01.01.07" --codes "UTR_01.01.07.01" "ПР_UTR_01.01.07" --json \
  > /tmp/pilot-utr-sources.json

.venv/bin/python "$SKILL/scripts/collect_sources.py" \
  --topic "UPO_01.03 единица оборудования" \
  --codes "dev_UPO_01.03_46" "FTT_UPO_01.03.012" "SND-UPO_01.03_001" --json \
  > /tmp/pilot-upo-sources.json

.venv/bin/python "$SKILL/scripts/collect_sources.py" \
  --topic "UDO договорная документация роли" \
  --codes "ПР_UDO_01.01.01" "UDO_01.01.01" \
  --meeting-query "UDO договорная документация протокол совет решение" --json \
  > /tmp/pilot-udo-sources.json
```

В каждом JSON гляньте `merged[].page_id`: должны встречаться ожидаемые id из
`fixtures/acceptance_pilots.json`.

## 2. DeerFlow — три отдельных чата

Вставьте `prompt` из того же fixture (поле `pilots[].prompt`).
Сохраните полный ответ агента в три файла:

```text
eval/runs/utr-invest-program.md
eval/runs/upo-dev-trace.md
eval/runs/udo-docs-plus-meetings.md
```

(локально в репо или на rag — путь любой).

## 3. Автосчёт page_id

```bash
python3 skills/acceptance-scenario/scripts/score_scenario.py \
  eval/runs/utr-invest-program.md --pilot-id utr-invest-program
python3 skills/acceptance-scenario/scripts/score_scenario.py \
  eval/runs/upo-dev-trace.md --pilot-id upo-dev-trace
python3 skills/acceptance-scenario/scripts/score_scenario.py \
  eval/runs/udo-docs-plus-meetings.md --pilot-id udo-docs-plus-meetings
```

## 4. Ручной hallucinated

Откройте `eval/scorecard.template.md`. Для каждого шага из вывода scorer:
`grounded` / `gap_ok` / `hallucinated`. Перенесите цифры в сводную таблицу.

Пришлите сводку + спорные шаги — по ним решим, усиливать промпт или retrieval.
