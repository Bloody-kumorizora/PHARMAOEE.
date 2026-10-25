"""Проверка ввода: даты, числа, записи смен, простои, оборудование, смены по расписанию."""
import unittest
from datetime import date, datetime, timedelta

from pharmaoee_app import validation as v

TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 12, 0)


def prod(**kw):
    raw = dict(work_date="24.10.2026", shift="1 смена", planned="480", run="420", total="18000", good="17640",
               cycle="1,2", note="")
    raw.update(kw)
    return v.validate_production(raw, 1, TODAY)


def down(**kw):
    raw = dict(work_date="24.10.2026", shift="1 смена", start_date="24.10.2026", start_time="10:00",
               end_date="24.10.2026", end_time="10:30", ongoing=False, reason="Переналадка", comment="")
    raw.update(kw)
    return v.validate_downtime(raw, 1, NOW, {"Переналадка", "Не классифицировано"})


class ParsingTests(unittest.TestCase):
    def test_dates_iso_and_russian(self):
        self.assertEqual(v.parse_date("2026-10-05"), date(2026, 10, 5))
        self.assertEqual(v.parse_date("05.10.2026"), date(2026, 10, 5))
        self.assertEqual(v.parse_date("5.1.2026"), date(2026, 1, 5))

    def test_invalid_dates(self):
        for bad in ("", "2026-02-30", "31.04.2026", "завтра", "2026/10/05", "05-10-2026"):
            with self.subTest(bad=bad), self.assertRaises(v.FieldError):
                v.parse_date(bad)

    def test_times(self):
        self.assertEqual(v.parse_time("7:05").hour, 7)
        self.assertEqual(v.parse_time("23:59:30").second, 30)
        for bad in ("24:00", "12:60", "1200", ""):
            with self.subTest(bad=bad), self.assertRaises(v.FieldError):
                v.parse_time(bad)

    def test_numbers(self):
        self.assertEqual(v.parse_number("1,5", "x"), 1.5)
        self.assertEqual(v.parse_number("18 000", "x", integer=True), 18000)
        self.assertEqual(v.parse_number("18 000", "x", integer=True), 18000)
        for bad, integer in (("abc", False), ("1.5", True), ("", False), ("nan", False), ("inf", False)):
            with self.subTest(bad=bad), self.assertRaises(v.FieldError):
                v.parse_number(bad, "x", integer=integer)

    def test_date_range_inclusive_and_limited(self):
        self.assertEqual(list(v.dates_inclusive(date(2026, 10, 1), date(2026, 10, 3))),
                         [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)])
        self.assertEqual(list(v.dates_inclusive(date(2026, 10, 5), date(2026, 10, 5))), [date(2026, 10, 5)])
        with self.assertRaises(ValueError):
            list(v.dates_inclusive(date(2026, 10, 5), date(2026, 10, 1)))
        with self.assertRaises(v.FieldError):
            v.parse_date_range("01.01.2025", "02.01.2026", max_days=366)
        self.assertEqual(v.parse_date_range("01.01.2025", "01.01.2026", max_days=366)[1], date(2026, 1, 1))


class ProductionValidationTests(unittest.TestCase):
    def test_valid_record(self):
        data, report = prod()
        self.assertTrue(report.ok)
        self.assertEqual(data.total_count, 18000)
        self.assertEqual(data.ideal_cycle_sec, 1.2)
        self.assertEqual(report.warnings, [])

    def test_blocking_errors(self):
        cases = {
            "run>planned": dict(run="500"),
            "good>total": dict(good="18001"),
            "planned=0": dict(planned="0"),
            "planned>day": dict(planned="1500"),
            "cycle=0": dict(cycle="0"),
            "negative": dict(total="-1"),
            "empty": dict(total=""),
            "output without run": dict(run="0"),
            "bad shift": dict(shift="4 смена"),
            "bad date": dict(work_date="32.10.2026"),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                data, report = prod(**kw)
                self.assertIsNone(data)
                self.assertFalse(report.ok)

    def test_missing_equipment(self):
        data, report = v.validate_production(dict(work_date="24.10.2026", shift="1 смена", planned="480", run="420",
                                                  total="1", good="1", cycle="1"), None, TODAY)
        self.assertIsNone(data)
        self.assertIn("Выберите оборудование.", report.errors)

    def test_performance_over_100_is_warning_not_error(self):
        data, report = prod(total="30000", good="29000")
        self.assertIsNotNone(data)
        self.assertTrue(any("выше 100%" in w for w in report.warnings))

    def test_future_date_warns(self):
        data, report = prod(work_date="26.10.2026")
        self.assertIsNotNone(data)
        self.assertTrue(any("будущем" in w for w in report.warnings))

    def test_zero_run_with_zero_output_allowed_with_warning(self):
        data, report = prod(run="0", total="0", good="0")
        self.assertIsNotNone(data)
        self.assertTrue(report.warnings)


class DowntimeValidationTests(unittest.TestCase):
    def test_valid_interval(self):
        data, report = down()
        self.assertTrue(report.ok)
        self.assertEqual(data.minutes, 30)

    def test_end_before_start_same_day_suggests_next_day(self):
        data, report = down(start_time="23:50", end_time="00:20")
        self.assertIsNone(data)
        self.assertIn("через полночь", report.errors[0])

    def test_midnight_crossing_with_next_day(self):
        data, report = down(start_time="23:50", end_date="25.10.2026", end_time="00:20")
        self.assertTrue(report.ok)
        self.assertEqual(data.minutes, 30)

    def test_zero_length_rejected(self):
        data, report = down(end_time="10:00")
        self.assertIsNone(data)

    def test_future_end_rejected_and_ongoing_allowed(self):
        data, _ = down(start_date="25.10.2026", start_time="11:00", end_date="25.10.2026", end_time="13:00")
        self.assertIsNone(data)
        data, report = down(start_date="25.10.2026", start_time="11:00", ongoing=True, end_date="", end_time="")
        self.assertTrue(report.ok)
        self.assertIsNone(data.end_at)
        self.assertIsNone(data.minutes)

    def test_future_start_rejected(self):
        data, _ = down(start_date="25.10.2026", start_time="13:00", ongoing=True)
        self.assertIsNone(data)

    def test_empty_end_requires_ongoing_flag(self):
        data, report = down(end_time="")
        self.assertIsNone(data)
        self.assertIn("Простой продолжается", report.errors[0])

    def test_too_long_and_long_warning(self):
        data, _ = down(end_date="02.11.2026")
        self.assertIsNone(data)
        data, report = down(start_date="23.10.2026", end_date="24.10.2026", end_time="09:00", work_date="23.10.2026")
        self.assertIsNotNone(data)
        self.assertTrue(any("12 часов" in w for w in report.warnings))

    def test_unknown_reason_rejected(self):
        data, _ = down(reason="Нет такой")
        self.assertIsNone(data)


class ShiftScheduleTests(unittest.TestCase):
    def test_shift_for_boundaries(self):
        cases = {
            datetime(2026, 10, 25, 7, 0): (date(2026, 10, 25), "1 смена"),
            datetime(2026, 10, 25, 14, 59): (date(2026, 10, 25), "1 смена"),
            datetime(2026, 10, 25, 15, 0): (date(2026, 10, 25), "2 смена"),
            datetime(2026, 10, 25, 23, 30): (date(2026, 10, 25), "3 смена"),
            datetime(2026, 10, 25, 0, 30): (date(2026, 10, 24), "3 смена"),
            datetime(2026, 10, 25, 6, 59): (date(2026, 10, 24), "3 смена"),
        }
        for moment, expected in cases.items():
            with self.subTest(moment=moment):
                self.assertEqual(v.shift_for(moment), expected)

    def test_night_shift_bounds_cross_midnight(self):
        start, end = v.shift_bounds(date(2026, 10, 24), "3 смена")
        self.assertEqual(start, datetime(2026, 10, 24, 23, 0))
        self.assertEqual(end - start, timedelta(hours=8))
        self.assertEqual(end.date(), date(2026, 10, 25))


class EquipmentValidationTests(unittest.TestCase):
    def test_name_normalized_and_cycle_optional(self):
        data, report = v.validate_equipment({"name": "  Линия   1 ", "cycle": ""})
        self.assertEqual(data.name, "Линия 1")
        self.assertIsNone(data.ideal_cycle_sec)

    def test_errors(self):
        for raw in ({"name": ""}, {"name": "x", "cycle": "0"}, {"name": "x", "cycle": "abc"}, {"name": "x" * 130}):
            with self.subTest(raw=raw):
                data, report = v.validate_equipment(raw)
                self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
