"""Тесты validate_report.py и render_report.py.

Запуск: python3 -m unittest discover -s ai-monitor/tests
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import render_report  # noqa: E402
import validate_report  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "valid_report.json")
ARGS = ["--agent", "NA", "--zone", "US,CA", "--period-start", "2026-01-05", "--period-end", "2026-01-12"]


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def run_validator(data, extra_args=(), raw=None, previous_dir=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "report.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(data, ensure_ascii=False))
        args = list(ARGS) + list(extra_args) + [path]
        if previous_dir:
            args = ["--previous-dir", previous_dir] + args
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = validate_report.main(args)
        return code, buf.getvalue()


class ValidatorTests(unittest.TestCase):
    def test_valid_fixture_passes(self):
        code, out = run_validator(load_fixture())
        self.assertEqual(code, 0, out)
        self.assertIn("ПРОЙДЕНА", out)

    def test_empty_items_pass(self):
        data = load_fixture()
        data["items"] = []
        code, out = run_validator(data)
        self.assertEqual(code, 0, out)

    def test_markdown_fence_rejected(self):
        raw = "```json\n" + json.dumps(load_fixture(), ensure_ascii=False) + "\n```"
        code, out = run_validator(None, raw=raw)
        self.assertEqual(code, 1)
        self.assertIn("markdown", out)

    def test_text_around_json_rejected(self):
        raw = "Вот отчёт:\n" + json.dumps(load_fixture(), ensure_ascii=False)
        code, out = run_validator(None, raw=raw)
        self.assertEqual(code, 1)
        self.assertIn("невалидный JSON", out)

    def assert_error(self, mutate, needle):
        data = load_fixture()
        mutate(data)
        code, out = run_validator(data)
        self.assertEqual(code, 1, out)
        self.assertIn(needle, out)

    def test_bad_enum(self):
        self.assert_error(lambda d: d["items"][0].update(category="санкции"), "category")

    def test_missing_field(self):
        self.assert_error(lambda d: d["items"][0].pop("follow_up"), "нет обязательных полей")

    def test_extra_field(self):
        self.assert_error(lambda d: d["items"][0].update(comment="x"), "лишние поля")

    def test_numbering(self):
        self.assert_error(lambda d: d["items"][1].update(item_id="NA-20260112-003"), "нумерация")

    def test_item_id_date(self):
        self.assert_error(lambda d: d["items"][0].update(item_id="NA-20260111-001"), "generated_at")

    def test_out_of_zone(self):
        self.assert_error(lambda d: d["items"][0].update(jurisdiction="GB"), "вне зоны")

    def test_wrong_agent(self):
        self.assert_error(lambda d: d.update(agent_code="CL"), "agent_code")

    def test_secondary_needs_two_sources(self):
        self.assert_error(lambda d: d["items"][1].update(secondary_urls=["https://example.org/a"]),
                          "не менее двух")

    def test_primary_needs_url(self):
        self.assert_error(lambda d: d["items"][0].update(source_url=None), "source_url = null")

    def test_significance_range(self):
        self.assert_error(lambda d: d["items"][0].update(significance=6), "significance")

    def test_significance_not_bool(self):
        self.assert_error(lambda d: d["items"][0].update(significance=True), "significance")

    def test_bad_date(self):
        self.assert_error(lambda d: d["items"][0].update(date_published="08.01.2026"), "date_published")

    def test_empty_string_instead_of_null(self):
        self.assert_error(lambda d: d["items"][0].update(identifier=""), "identifier")

    def test_period_mismatch(self):
        self.assert_error(lambda d: d["period"].update(start="2026-01-06"), "period.start")

    def test_out_of_period_is_warning_not_error(self):
        data = load_fixture()
        data["items"][0]["date_published"] = "2025-12-01"
        code, out = run_validator(data)
        self.assertEqual(code, 0, out)
        self.assertIn("вне периода", out)

    def test_limit_mentioned_without_incomplete_prefix_warns(self):
        data = load_fixture()
        data["coverage_note"] = "Проверены источники А и Б; исчерпан лимит поиска."
        code, out = run_validator(data)
        self.assertEqual(code, 0, out)
        self.assertIn("ОХВАТ НЕПОЛНЫЙ", out)
        data["coverage_note"] = "ОХВАТ НЕПОЛНЫЙ: исчерпан лимит поиска."
        code, out = run_validator(data)
        self.assertNotIn("WARN  coverage_note", out)

    def test_repeat_from_previous_report_warns(self):
        with tempfile.TemporaryDirectory() as prev:
            old = load_fixture()
            old["generated_at"] = "2026-01-09"
            with open(os.path.join(prev, "2026-01-09.json"), "w", encoding="utf-8") as fh:
                json.dump(old, fh, ensure_ascii=False)
            code, out = run_validator(load_fixture(), previous_dir=prev)
        self.assertEqual(code, 0, out)
        self.assertIn("уже был в отчёте", out)


class RenderTests(unittest.TestCase):
    def test_body_contains_only_report_data(self):
        data = load_fixture()
        text = render_report.body(data)
        self.assertIn("NA-20260112-001", text)
        self.assertIn("ГЛАВНОЕ", text)
        self.assertIn("https://example.org/test-order", text)
        self.assertIn("URL первоисточника не установлен", text)
        self.assertIn(data["coverage_note"], text)
        self.assertLess(text.index("САНКЦИИ ЗА ОШИБКИ ИИ"), text.index("ПРОФЕССИОНАЛЬНАЯ ЭТИКА"))

    def test_empty_report(self):
        data = load_fixture()
        data["items"] = []
        text = render_report.body(data)
        self.assertIn("За период новшеств не найдено", text)

    def test_incomplete_coverage_is_not_reported_as_no_news(self):
        data = load_fixture()
        data["items"] = []
        data["coverage_note"] = "ОХВАТ НЕПОЛНЫЙ: исчерпан лимит Legal Data Hunter."
        text = render_report.body(data)
        self.assertNotIn("За период новшеств не найдено", text)
        self.assertIn("Проверка неполная", text)
        self.assertIn("охват неполный", render_report.subject(data))

    def test_subject(self):
        subj = render_report.subject(load_fixture())
        self.assertIn("США и Канада (агент NA)", subj)
        self.assertIn("материалов 2", subj)
        self.assertIn("значимость 4–5: 1", subj)
        self.assertNotIn("\n", subj)


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json_and_matches_validator_keys(self):
        with open(os.path.join(ROOT, "report.schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        self.assertEqual(schema["required"], validate_report.TOP_KEYS)
        item = schema["$defs"]["item"]
        self.assertEqual(item["required"], validate_report.ITEM_KEYS)
        for field, values in validate_report.ENUMS.items():
            self.assertEqual(item["properties"][field]["enum"], values, field)

    def test_fixture_against_schema_if_jsonschema_installed(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema не установлен")
        with open(os.path.join(ROOT, "report.schema.json"), encoding="utf-8") as fh:
            schema = json.load(fh)
        jsonschema.validate(load_fixture(), schema)
        bad = copy.deepcopy(load_fixture())
        bad["items"][1]["secondary_urls"] = ["https://example.org/a"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


if __name__ == "__main__":
    unittest.main()
