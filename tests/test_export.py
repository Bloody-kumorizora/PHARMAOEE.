"""Экспорт CSV: совместимость с Excel, фильтры, предупреждение о демо-данных, защита от формул."""
import csv
import unittest
from datetime import date, datetime, timedelta

from pharmaoee_app import analytics, export
from pharmaoee_app.db import Filters
from pharmaoee_app.validation import EquipmentInput

from tests.helpers import DBTestCase


def read(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    return raw, list(csv.reader(text.splitlines(), delimiter=";"))


class ExportTests(DBTestCase):
    def setUp(self):
        super().setUp()
        self.eq = self.equipment("Линия «Альфа»")
        self.production(self.eq, date(2026, 10, 1), planned=480, run=420, total=18000, good=17640, cycle=1.2)
        self.production(self.eq, date(2026, 10, 5))
        t = datetime(2026, 10, 1, 10, 0)
        self.downtime(self.eq, t, t + timedelta(minutes=15))

    def snap(self, start=date(2026, 10, 1), end=date(2026, 10, 1)):
        return analytics.load_snapshot(self.db, Filters(start, end))

    def test_production_csv_is_excel_friendly_and_filtered(self):
        path = self.tmp / "p.csv"
        count = export.export_production(path, self.snap(), "все")
        raw, rows = read(path)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))           # BOM для Excel
        self.assertEqual(count, 1)                                  # 5 октября не попало в период
        header = rows.index(export.PRODUCTION_COLUMNS)
        data = rows[header + 1]
        self.assertEqual(data[1], "01.10.2026")
        self.assertEqual(data[3], "Линия «Альфа»")
        self.assertEqual(data[10], "87,5")                          # доступность, десятичная запятая
        self.assertEqual(data[13], "73,5")                          # OEE
        self.assertIn("Период (по дате смены, включительно)", rows[1][0])
        self.assertEqual(rows[1][1], "01.10.2026 — 01.10.2026")
        self.assertTrue(any("не является валидированным GMP-отчётом" in r[0] for r in rows if r))

    def test_demo_warning_present_only_when_needed(self):
        path = self.tmp / "p.csv"
        export.export_production(path, self.snap(), "все")
        self.assertFalse(any(r and r[0] == "ВНИМАНИЕ" for r in read(path)[1]))
        self.production(self.eq, date(2026, 10, 1), shift="2 смена", source="demo")
        export.export_production(path, self.snap(), "все")
        self.assertTrue(any(r and r[0] == "ВНИМАНИЕ" for r in read(path)[1]))

    def test_formula_injection_escaped(self):
        bad = self.db.add_equipment(EquipmentInput("=HYPERLINK(\"x\")"))
        self.production(bad, date(2026, 10, 1))
        path = self.tmp / "p.csv"
        export.export_production(path, self.snap(), "все")
        names = [r[3] for r in read(path)[1] if len(r) > 3]
        self.assertIn("'=HYPERLINK(\"x\")", names)

    def test_downtimes_and_summary(self):
        path = self.tmp / "d.csv"
        self.assertEqual(export.export_downtimes(path, self.snap(), "все"), 1)
        rows = read(path)[1]
        data = rows[rows.index(export.DOWNTIME_COLUMNS) + 1]
        self.assertEqual(data[6], "15,0")
        summary = self.tmp / "s.csv"
        export.export_summary(summary, self.snap(date(2026, 10, 1), date(2026, 10, 5)), "все")
        rows = read(summary)[1]
        titles = [r[0] for r in rows if r]
        for section in ("ИТОГО ЗА ПЕРИОД", "ПО ДНЯМ", "ПО СМЕНАМ", "ПО ОБОРУДОВАНИЮ", "ПРИЧИНЫ ПРОСТОЕВ"):
            self.assertIn(section, titles)

    def test_write_error_is_reported_and_no_partial_file(self):
        target = self.tmp / "folder.csv"
        target.mkdir()
        with self.assertRaises(export.ExportError):
            export.export_production(target, self.snap(), "все")
        self.assertFalse((self.tmp / "folder.csv.tmp").exists())


if __name__ == "__main__":
    unittest.main()
