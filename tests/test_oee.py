"""Расчёт OEE: стандартные и граничные случаи, контрольные примеры, посчитанные вручную."""
import unittest

from pharmaoee_app import formatting, oee


class SingleRecordTests(unittest.TestCase):
    def test_reference_example_checked_by_hand(self):
        # План 480 мин, работа 420 мин, 18000 шт., годных 17640, цикл 1,2 с.
        # A = 420/480 = 0,875; P = 18000·1,2/(420·60) = 21600/25200 = 0,857142…;
        # Q = 17640/18000 = 0,98; OEE = 17640·1,2/(480·60) = 21168/28800 = 0,735.
        r = oee.calculate(480, 420, 18000, 17640, 1.2)
        self.assertAlmostEqual(r.availability, 0.875)
        self.assertAlmostEqual(r.performance, 6 / 7)
        self.assertAlmostEqual(r.quality, 0.98)
        self.assertAlmostEqual(r.oee, 0.735)
        self.assertEqual(r.defect_count, 360)
        self.assertEqual(r.flags, frozenset())

    def test_second_hand_example(self):
        # A = 90/100 = 0,9; P = 72·60/(90·60) = 0,8; Q = 68/72; OEE = 68·60/(100·60) = 0,68.
        r = oee.calculate(100, 90, 72, 68, 60)
        self.assertAlmostEqual(r.availability, 0.9)
        self.assertAlmostEqual(r.performance, 0.8)
        self.assertAlmostEqual(r.quality, 68 / 72)
        self.assertAlmostEqual(r.oee, 0.68)

    def test_product_of_factors_equals_oee(self):
        for args in [(480, 420, 18000, 17640, 1.2), (60, 30, 10, 3, 2.5), (1440, 1440, 86400, 86400, 1)]:
            r = oee.calculate(*args)
            self.assertAlmostEqual(r.availability * r.performance * r.quality, r.oee)

    def test_perfect_shift_is_100_percent(self):
        r = oee.calculate(100, 100, 100, 100, 60)
        self.assertEqual((r.availability, r.performance, r.quality, r.oee), (1.0, 1.0, 1.0, 1.0))

    def test_performance_above_100_is_not_clipped_and_flagged(self):
        r = oee.calculate(60, 60, 120, 120, 60)
        self.assertAlmostEqual(r.performance, 2.0)
        self.assertAlmostEqual(r.oee, 2.0)
        self.assertIn(oee.FLAG_PERFORMANCE_OVER, r.flags)
        self.assertTrue(any("выше 100%" in w for w in r.warnings))

    def test_availability_above_100_is_flagged(self):
        r = oee.calculate(100, 120, 10, 10, 60)
        self.assertAlmostEqual(r.availability, 1.2)
        self.assertIn(oee.FLAG_AVAILABILITY_OVER, r.flags)

    def test_good_over_total_is_flagged(self):
        r = oee.calculate(100, 90, 10, 12, 60)
        self.assertIn(oee.FLAG_GOOD_OVER_TOTAL, r.flags)

    def test_zero_planned_time(self):
        r = oee.calculate(0, 0, 0, 0, 60)
        self.assertIsNone(r.availability)
        self.assertIsNone(r.oee)
        self.assertIn(oee.FLAG_NO_PLANNED, r.flags)

    def test_zero_run_time_gives_zero_oee_and_undefined_performance(self):
        r = oee.calculate(480, 0, 0, 0, 1.2)
        self.assertEqual(r.availability, 0.0)
        self.assertIsNone(r.performance)
        self.assertIsNone(r.quality)
        self.assertEqual(r.oee, 0.0)

    def test_zero_output(self):
        r = oee.calculate(480, 420, 0, 0, 1.2)
        self.assertEqual(r.performance, 0.0)
        self.assertIsNone(r.quality)
        self.assertEqual(r.oee, 0.0)
        self.assertIn(oee.FLAG_NO_OUTPUT, r.flags)

    def test_missing_ideal_cycle(self):
        r = oee.calculate(480, 420, 100, 90, 0)
        self.assertIsNone(r.performance)
        self.assertIsNone(r.oee)
        self.assertAlmostEqual(r.quality, 0.9)
        self.assertIn(oee.FLAG_NO_CYCLE, r.flags)

    def test_negative_input_is_not_calculated(self):
        r = oee.calculate(480, -10, 100, 90, 1)
        self.assertIsNone(r.availability)
        self.assertIsNone(r.oee)
        self.assertIn(oee.FLAG_NEGATIVE, r.flags)

    def test_very_large_values(self):
        r = oee.calculate(1440, 1440, 50_000_000, 49_000_000, 0.001)
        self.assertAlmostEqual(r.performance, 50_000_000 * 0.001 / (1440 * 60))
        self.assertAlmostEqual(r.quality, 0.98)

    def test_ratio_status(self):
        self.assertEqual(oee.ratio_status(0.5, 0.6), "bad")
        self.assertEqual(oee.ratio_status(0.7, 0.6), "ok")
        self.assertEqual(oee.ratio_status(1.05, 0.6), "over")
        self.assertIsNone(oee.ratio_status(None, 0.6))


class AggregateTests(unittest.TestCase):
    def test_empty_period(self):
        r = oee.aggregate([])
        self.assertFalse(r.has_data)
        self.assertIsNone(r.oee)

    def test_aggregate_uses_sums_not_average_of_ratios(self):
        rows = [dict(planned_min=480, run_min=480, total_count=24000, good_count=24000, ideal_cycle_sec=1.2),
                dict(planned_min=480, run_min=240, total_count=6000, good_count=5400, ideal_cycle_sec=1.2)]
        r = oee.aggregate(rows)
        self.assertAlmostEqual(r.availability, 720 / 960)
        self.assertAlmostEqual(r.performance, 30000 * 1.2 / (720 * 60))
        self.assertAlmostEqual(r.quality, 29400 / 30000)
        self.assertAlmostEqual(r.oee, 29400 * 1.2 / (960 * 60))

    def test_mixed_cycles_keep_product_consistent(self):
        rows = [dict(planned_min=480, run_min=400, total_count=20000, good_count=19000, ideal_cycle_sec=1.0),
                dict(planned_min=480, run_min=450, total_count=5000, good_count=4990, ideal_cycle_sec=4.0)]
        r = oee.aggregate(rows)
        self.assertAlmostEqual(r.availability * r.performance * r.quality, r.oee)
        self.assertAlmostEqual(r.good_share, 23990 / 25000)
        self.assertNotAlmostEqual(r.quality, r.good_share)

    def test_none_values_are_treated_as_zero(self):
        r = oee.aggregate([dict(planned_min=480, run_min=None, total_count=None, good_count=None, ideal_cycle_sec=1)])
        self.assertEqual(r.run_min, 0)
        self.assertEqual(r.oee, 0)


class FormattingTests(unittest.TestCase):
    def test_percent_rounding_and_comma(self):
        self.assertEqual(formatting.pct(0.8534), "85,3%")
        self.assertEqual(formatting.pct(0.85349), "85,3%")
        self.assertEqual(formatting.pct(1.3333), "133,3%")
        self.assertEqual(formatting.pct(None), "—")

    def test_minutes(self):
        self.assertEqual(formatting.minutes(45), "45 мин")
        self.assertEqual(formatting.minutes(65), "1 ч 05 мин")
        self.assertEqual(formatting.minutes(None), "—")

    def test_dates(self):
        self.assertEqual(formatting.ru_date("2026-10-05"), "05.10.2026")
        self.assertEqual(formatting.ru_datetime("2026-10-05 07:03:00"), "05.10.2026 07:03")


if __name__ == "__main__":
    unittest.main()
