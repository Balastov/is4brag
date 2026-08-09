#!/usr/bin/env python3
"""
Прокси-скрипт: делегирует поиск по Спецификациям требований в kisu_metro_search.py.

Использование (идентично kisu_metro_search.py):
    python3 spec_search.py "текст запроса" [--top-k 10] [--json] [--verbose]

Спецификации требований входят в раздел «Стадии проекта» (отдельного раздела нет).
"""

import sys
import os
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METRO_SEARCH = os.path.join(
    SCRIPT_DIR, "..", "..", "kisu-metro", "scripts", "kisu_metro_search.py"
)
# Specs live under the Stages tree in Confluence / canonical store.
SPEC_SECTION = "Стадии проекта"

if __name__ == "__main__":
    # Запрос должен быть sys.argv[1] для kisu_metro_search.py.
    # Вставляем --section ПОСЛЕ запроса, а не перед ним.
    cmd = [sys.executable, METRO_SEARCH]
    cmd.extend(sys.argv[1:2])                    # запрос
    cmd.extend(["--section", SPEC_SECTION])      # фильтр раздела
    cmd.extend(sys.argv[2:])                     # остальные аргументы (--top-k, --json, ...)
    sys.exit(subprocess.call(cmd))
