"""Пересекающиеся интервалы простоев: без двойного учёта времени."""
import unittest
from datetime import datetime, timedelta

from pharmaoee_app.intervals import Interval, allocate, find_overlaps, overlaps, union_minutes

T0 = datetime(2026, 10, 1, 8, 0)


def iv(key, start_min, end_min, group="A"):
    return Interval(key, group, T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min))


class IntervalTests(unittest.TestCase):
    def test_no_overlap(self):
        items = [iv(1, 0, 10), iv(2, 20, 30)]
        self.assertEqual(allocate(items), {1: 10, 2: 10})
        self.assertEqual(find_overlaps(items), [])

    def test_touching_is_not_overlap(self):
        items = [iv(1, 0, 10), iv(2, 10, 20)]
        self.assertFalse(overlaps(items[0].start, items[0].end, items[1].start, items[1].end))
        self.assertEqual(union_minutes(items), 20)

    def test_partial_overlap_goes_to_earlier(self):
        items = [iv(2, 5, 20), iv(1, 0, 10)]
        self.assertEqual(allocate(items), {1: 10, 2: 10})
        self.assertEqual(union_minutes(items), 20)
        self.assertEqual(len(find_overlaps(items)), 1)

    def test_contained_interval_counts_zero(self):
        items = [iv(1, 0, 60), iv(2, 10, 20), iv(3, 30, 40)]
        self.assertEqual(allocate(items), {1: 60, 2: 0, 3: 0})
        self.assertEqual(len(find_overlaps(items)), 2)

    def test_same_start_lower_id_wins(self):
        self.assertEqual(allocate([iv(7, 0, 10), iv(3, 0, 10)]), {3: 10, 7: 0})

    def test_groups_are_independent(self):
        items = [iv(1, 0, 10, "A"), iv(2, 0, 10, "B")]
        self.assertEqual(union_minutes(items), 20)
        self.assertEqual(find_overlaps(items), [])

    def test_across_midnight(self):
        start = datetime(2026, 10, 1, 23, 50)
        items = [Interval(1, "A", start, start + timedelta(minutes=30))]
        self.assertEqual(union_minutes(items), 30)


if __name__ == "__main__":
    unittest.main()
