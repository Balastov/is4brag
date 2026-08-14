import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "acceptance-scenario"
    / "scripts"
    / "score_scenario.py"
)
SPEC = importlib.util.spec_from_file_location("score_scenario", SCRIPT)
score_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score_mod)


class AcceptanceScoreTests(unittest.TestCase):
    def test_counts_page_id_gap_and_ungrounded(self):
        markdown = """
## 6. Шаги сценария

| # | Действие | Ожидаемый результат | Источник |
|---|---|---|---|
| 1 | Создать объект | Статус На рассмотрение | ПР page_id=12366437 |
| 2 | Неизвестный шаг | Успех |  |
| 3 | Роль не найдена | — | НЕ НАЙДЕНО В ИСТОЧНИКАХ |

## 7. Негативные / граничные проверки

| # | Действие | Ожидаемый результат | Источник |
|---|---|---|---|
| 1 | | | |
"""
        result = score_mod.score(markdown)
        self.assertEqual(result["total_steps"], 3)
        self.assertEqual(result["with_page_id"], 1)
        self.assertEqual(result["with_page_id_pct"], 33.3)
        self.assertEqual(result["explicit_gaps"], 1)
        self.assertEqual(result["ungrounded_candidates"], 1)
        self.assertEqual(result["unique_page_ids"], ["12366437"])


if __name__ == "__main__":
    unittest.main()
