"""Демо-генератор: воспроизводимость, отсутствие дублей, лимиты, безопасная очистка."""
import unittest
from datetime import date, datetime

from pharmaoee_app import demo
from pharmaoee_app.db import Database, Filters
from pharmaoee_app.intervals import Interval, find_overlaps
from pharmaoee_app.validation import shift_bounds

from tests.helpers import DBTestCase

NOW = datetime(2026, 10, 25, 12, 0)
ALL_SHIFTS = ("1 смена", "2 смена", "3 смена")


def rows(db: Database):
    prod = sorted((r["equipment_name"], r["work_date"], r["shift"], r["run_min"], r["total_count"], r["good_count"])
                  for r in db.list_production())
    down = sorted((d["equipment_name"], d["start_at"], d["end_at"], d["reason"]) for d in db.list_downtimes())
    return prod, down


class DemoTests(DBTestCase):
    def test_reproducible_for_same_settings(self):
        request = demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 7), shifts=ALL_SHIFTS)
        demo.generate(self.db, request, now=NOW)
        other = Database(self.tmp / "other.db")
        other.initialize()
        demo.generate(other, request, now=NOW)
        self.assertEqual(rows(self.db), rows(other))

    def test_different_seed_gives_different_data(self):
        a = demo.plan(demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 3), seed=1))
        b = demo.plan(demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 3), seed=2))
        self.assertNotEqual([s.total_count for s in a], [s.total_count for s in b])

    def test_overlapping_periods_give_same_records(self):
        demo.generate(self.db, demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 10)), now=NOW)
        result = demo.generate(self.db, demo.DemoRequest(date(2026, 10, 5), date(2026, 10, 15)), now=NOW)
        self.assertEqual(result.skipped, 6 * 3)
        other = Database(self.tmp / "other.db")
        other.initialize()
        demo.generate(other, demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 15)), now=NOW)
        self.assertEqual(rows(self.db), rows(other))

    def test_rerun_does_not_duplicate(self):
        request = demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 3))
        first = demo.generate(self.db, request, now=NOW)
        count = self.db.count_production()
        second = demo.generate(self.db, request, now=NOW)
        self.assertEqual(first.created_shifts, 9)
        self.assertEqual(second.created_shifts, 0)
        self.assertEqual(second.skipped, 9)
        self.assertEqual(self.db.count_production(), count)

    def test_existing_manual_record_is_skipped_not_overwritten(self):
        eq = demo.ensure_demo_equipment(self.db)[0]
        self.production(eq, date(2026, 10, 2), run=111)
        demo.generate(self.db, demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 3)), now=NOW)
        manual = self.db.list_production(Filters(date(2026, 10, 2), date(2026, 10, 2), eq, "1 смена"))
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["run_min"], 111)
        self.assertEqual(manual[0]["source"], "manual")

    def test_limits_and_future_dates(self):
        with self.assertRaises(ValueError):
            demo.DemoRequest(date(2025, 1, 1), date(2026, 1, 2)).validate(date(2026, 10, 25))
        demo.DemoRequest(date(2025, 1, 1), date(2026, 1, 1)).validate(date(2026, 10, 25))   # ровно 366 дней
        with self.assertRaises(ValueError):
            demo.DemoRequest(date(2026, 10, 20), date(2026, 10, 26)).validate(date(2026, 10, 25))
        with self.assertRaises(ValueError):
            demo.DemoRequest(date(2026, 10, 3), date(2026, 10, 1)).validate(date(2026, 10, 25))
        with self.assertRaises(ValueError):
            demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 1), shifts=()).validate(date(2026, 10, 25))

    def test_unfinished_shifts_of_today_are_skipped(self):
        result = demo.generate(self.db, demo.DemoRequest(date(2026, 10, 25), date(2026, 10, 25), shifts=ALL_SHIFTS),
                               now=NOW)
        # В 12:00 ни одна смена 25 октября ещё не закончилась.
        self.assertEqual(result.created_shifts, 0)
        self.assertEqual(result.skipped_unfinished, 9)

    def test_generated_data_is_consistent(self):
        demo.generate(self.db, demo.DemoRequest(date(2026, 9, 1), date(2026, 10, 24), shifts=ALL_SHIFTS), now=NOW)
        downs = self.db.list_downtimes()
        from pharmaoee_app.db import parse_ts
        intervals = [Interval(d["id"], d["equipment_id"], parse_ts(d["start_at"]), parse_ts(d["end_at"])) for d in downs]
        self.assertEqual(find_overlaps(intervals), [])
        by_shift = {}
        for d in downs:
            key = (d["equipment_id"], d["work_date"], d["shift"])
            start, end = shift_bounds(date.fromisoformat(d["work_date"]), d["shift"])
            self.assertGreaterEqual(parse_ts(d["start_at"]), start)
            self.assertLessEqual(parse_ts(d["end_at"]), end)
            by_shift[key] = by_shift.get(key, 0) + (parse_ts(d["end_at"]) - parse_ts(d["start_at"])).total_seconds() / 60
        for r in self.db.list_production():
            key = (r["equipment_id"], r["work_date"], r["shift"])
            self.assertAlmostEqual(r["planned_min"] - r["run_min"], by_shift.get(key, 0))
            self.assertLessEqual(r["good_count"], r["total_count"])
            self.assertLessEqual(r["total_count"] * r["ideal_cycle_sec"], r["run_min"] * 60)   # P <= 100%
            self.assertEqual(r["source"], "demo")
        night = [d for d in downs if d["shift"] == "3 смена" and d["start_at"][:10] != d["work_date"]]
        self.assertTrue(night, "ночные смены должны давать простои после полуночи")

    def test_clear_demo_keeps_manual_data(self):
        manual_eq = self.equipment("Реальная линия")
        self.production(manual_eq, date(2026, 10, 2))
        demo.generate(self.db, demo.DemoRequest(date(2026, 10, 1), date(2026, 10, 3)), now=NOW)
        counts = self.db.delete_generated("demo")
        self.assertEqual(counts["production"], 9)
        self.assertEqual(counts["equipment"], 3)
        remaining = self.db.list_production()
        self.assertEqual([r["equipment_name"] for r in remaining], ["Реальная линия"])
        self.assertEqual([e["name"] for e in self.db.list_equipment()], ["Реальная линия"])

    def test_year_generation_speed(self):
        import time
        t0 = time.perf_counter()
        result = demo.generate(self.db, demo.DemoRequest(date(2025, 10, 24), date(2026, 10, 24), shifts=ALL_SHIFTS),
                               now=NOW)
        elapsed = time.perf_counter() - t0
        self.assertEqual(result.created_shifts, 366 * 9)
        self.assertLess(elapsed, 30, f"генерация года заняла {elapsed:.1f} с")


if __name__ == "__main__":
    unittest.main()
