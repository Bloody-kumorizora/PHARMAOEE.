"""Аналитика периода: выбранный день, разрывы, причины без двойного счёта, проверки данных."""
import unittest
from datetime import date, datetime, timedelta

from pharmaoee_app import analytics, oee
from pharmaoee_app.db import Filters

from tests.helpers import DBTestCase


class SnapshotTests(DBTestCase):
    def setUp(self):
        super().setUp()
        self.eq = self.equipment()
        self.production(self.eq, date(2026, 10, 1), run=450, total=20000, good=19800)
        self.production(self.eq, date(2026, 10, 3), run=400, total=18000, good=17000)
        self.production(self.eq, date(2026, 10, 3), shift="2 смена", run=480, total=24000, good=24000)

    def snap(self, start, end, **kw):
        return analytics.load_snapshot(self.db, Filters(start, end, **kw), now=datetime(2026, 10, 25, 12, 0))

    def test_selected_past_day_is_not_replaced_by_today(self):
        s = self.snap(date(2026, 10, 1), date(2026, 10, 1))
        self.assertEqual([r["work_date"] for r in s.production], ["2026-10-01"])
        expected = oee.calculate(480, 450, 20000, 19800, 1.2)
        self.assertAlmostEqual(s.total.oee, expected.oee)

    def test_kpi_table_and_chart_use_same_records(self):
        s = self.snap(date(2026, 10, 1), date(2026, 10, 3))
        daily = s.daily()
        self.assertEqual([d for d, _ in daily], [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)])
        self.assertFalse(daily[1][1].has_data)          # 2 октября — разрыв, а не ноль
        self.assertEqual(sum(r.records for _, r in daily), len(s.production))
        self.assertEqual(sum(r.total_count for _, r in daily), s.total.total_count)

    def test_period_from_data_when_filters_open(self):
        s = analytics.load_snapshot(self.db, Filters())
        self.assertEqual(s.period, (date(2026, 10, 1), date(2026, 10, 3)))

    def test_shift_breakdown_order(self):
        s = self.snap(date(2026, 10, 1), date(2026, 10, 3))
        self.assertEqual([name for name, _ in s.by_shift()], ["1 смена", "2 смена"])

    def test_problem_records_threshold(self):
        s = self.snap(date(2026, 10, 1), date(2026, 10, 3))
        problems = s.problem_records(threshold=0.75)
        self.assertTrue(all(p["result"].oee < 0.75 or p["issues"] for p in problems))
        self.assertEqual(len(s.problem_records(threshold=0.01)), 0)

    def test_empty_period(self):
        s = self.snap(date(2026, 11, 1), date(2026, 11, 2))
        self.assertFalse(s.total.has_data)
        self.assertEqual(s.reasons(), [])
        self.assertEqual(s.data_issues(), [])
        self.assertEqual(len(s.daily()), 2)


class DowntimeAnalyticsTests(DBTestCase):
    def test_overlapping_legacy_records_not_double_counted(self):
        eq = self.equipment()
        t = datetime(2026, 10, 1, 10, 0)
        self.downtime(eq, t, t + timedelta(minutes=30), reason="Переналадка")
        # Пересечение можно получить только в обход проверки (старые данные) — вставляем напрямую.
        from pharmaoee_app.validation import DowntimeInput
        with self.db.transaction() as con:
            self.db.insert_downtime(con, DowntimeInput(eq, date(2026, 10, 1), "1 смена", t + timedelta(minutes=20),
                                                       t + timedelta(minutes=50), "Ожидание материала"))
        s = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 1)))
        self.assertEqual(s.downtime_min, 50)
        reasons = {r["reason"]: r["minutes"] for r in s.reasons()}
        self.assertEqual(reasons, {"Переналадка": 30, "Ожидание материала": 20})
        self.assertTrue(all(d["overlaps"] for d in s.downtimes))
        self.assertEqual(sum(1 for i in s.data_issues() if i.kind == "overlap"), 2)

    def test_open_downtime_counted_until_now_and_flagged_when_long(self):
        eq = self.equipment()
        start = datetime(2026, 10, 1, 10, 0)
        self.downtime(eq, start, None)
        s = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 1)),
                                    now=start + timedelta(hours=2))
        self.assertEqual(s.downtime_min, 120)
        self.assertEqual(len(s.open_downtimes), 1)
        later = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 1)),
                                        now=start + timedelta(days=2))
        self.assertTrue(any(i.kind == "open" for i in later.data_issues()))

    def test_consistency_and_duplicate_checks(self):
        eq = self.equipment()
        self.production(eq, date(2026, 10, 1), planned=480, run=470)
        t = datetime(2026, 10, 1, 8, 0)
        self.downtime(eq, t, t + timedelta(minutes=40))
        with self.db.transaction() as con:      # дубликат из старой версии (в v9 запрещён интерфейсом)
            from pharmaoee_app.validation import ProductionInput
            self.db.insert_production(con, ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))
        s = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 1)))
        kinds = {i.kind for i in s.data_issues()}
        self.assertIn("consistency", kinds)
        self.assertIn("duplicate", kinds)

    def test_repeated_problems(self):
        eq = self.equipment()
        for day in range(1, 4):
            t = datetime(2026, 10, day, 9, 0)
            self.downtime(eq, t, t + timedelta(minutes=10), reason="Механическая неисправность")
        s = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 3)))
        rows = s.repeated_problems()
        self.assertEqual(rows[0]["count"], 3)
        self.assertEqual(rows[0]["days"], 3)
        self.assertEqual(rows[0]["minutes"], 30)

    def test_downtime_filtered_by_shift_date(self):
        eq = self.equipment()
        start = datetime(2026, 10, 2, 0, 30)     # после полуночи, но смена от 1 октября
        self.downtime(eq, start, start + timedelta(minutes=20), day=date(2026, 10, 1), shift="3 смена")
        first = analytics.load_snapshot(self.db, Filters(date(2026, 10, 1), date(2026, 10, 1)))
        second = analytics.load_snapshot(self.db, Filters(date(2026, 10, 2), date(2026, 10, 2)))
        self.assertEqual(len(first.downtimes), 1)
        self.assertEqual(len(second.downtimes), 0)


if __name__ == "__main__":
    unittest.main()
