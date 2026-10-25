"""События источников данных и симулятор смены."""
import unittest
from datetime import date, datetime, timedelta

from pharmaoee_app import oee
from pharmaoee_app.db import Filters, parse_ts
from pharmaoee_app.sources import (CONNECTION_LOST, LOST_NOTE, RUNNING, STOPPED, EventProcessor, ShiftSimulator,
                                   TelemetryEvent, validate_event)

from tests.helpers import DBTestCase

T = datetime(2026, 10, 20, 10, 0)


def event(eq, state, minutes, simulated=True):
    moment = T + timedelta(minutes=minutes)
    return TelemetryEvent(eq, state, moment, "тест", moment, simulated)


class ProcessorTests(DBTestCase):
    def setUp(self):
        super().setUp()
        self.eq = self.equipment()
        self.proc = EventProcessor(self.db)

    def test_stop_opens_downtime_with_shift_from_schedule(self):
        result = self.proc.apply(event(self.eq, STOPPED, 0))
        self.assertEqual(result.status, "applied")
        d = self.db.get_downtime(result.downtime_id)
        self.assertIsNone(d["end_at"])
        self.assertEqual((d["work_date"], d["shift"]), ("2026-10-20", "1 смена"))
        self.assertEqual(d["reason"], "Не классифицировано")
        self.assertEqual(d["source"], "simulator")

    def test_night_stop_belongs_to_previous_day_shift(self):
        moment = datetime(2026, 10, 21, 1, 30)
        result = self.proc.apply(TelemetryEvent(self.eq, STOPPED, moment, "тест", moment))
        d = self.db.get_downtime(result.downtime_id)
        self.assertEqual((d["work_date"], d["shift"]), ("2026-10-20", "3 смена"))

    def test_repeated_signal_is_ignored(self):
        self.proc.apply(event(self.eq, STOPPED, 0))
        again = self.proc.apply(event(self.eq, STOPPED, 1))
        self.assertEqual(again.status, "ignored")
        self.assertEqual(len(self.db.list_downtimes()), 1)

    def test_running_closes_downtime(self):
        opened = self.proc.apply(event(self.eq, STOPPED, 0)).downtime_id
        result = self.proc.apply(event(self.eq, RUNNING, 12))
        self.assertEqual(result.closed_downtime_id, opened)
        d = self.db.get_downtime(opened)
        self.assertEqual(parse_ts(d["end_at"]) - parse_ts(d["start_at"]), timedelta(minutes=12))
        self.assertEqual(self.db.current_state(self.eq)["state"], RUNNING)

    def test_connection_loss_is_not_downtime(self):
        self.proc.apply(event(self.eq, RUNNING, 0))
        result = self.proc.apply(event(self.eq, CONNECTION_LOST, 5))
        self.assertEqual(result.status, "applied")
        self.assertIsNone(result.downtime_id)
        self.assertEqual(self.db.list_downtimes(), [])

    def test_stop_continues_through_connection_loss(self):
        opened = self.proc.apply(event(self.eq, STOPPED, 0)).downtime_id
        self.proc.apply(event(self.eq, CONNECTION_LOST, 5))
        self.assertIsNone(self.db.get_downtime(opened)["end_at"])
        closed = self.proc.apply(event(self.eq, RUNNING, 20)).closed_downtime_id
        self.assertEqual(closed, opened)
        d = self.db.get_downtime(opened)
        self.assertEqual(d["end_at"], "2026-10-20 10:20:00")
        self.assertIn(LOST_NOTE, d["comment"])
        self.assertEqual(len(self.db.list_downtimes()), 1)

    def test_out_of_order_and_unknown_equipment_rejected(self):
        self.proc.apply(event(self.eq, STOPPED, 10))
        self.assertEqual(self.proc.apply(event(self.eq, RUNNING, 5)).status, "rejected")
        self.assertEqual(self.proc.apply(event(999, RUNNING, 5)).status, "rejected")

    def test_real_source_future_timestamp_rejected(self):
        future = TelemetryEvent(self.eq, RUNNING, datetime.now() + timedelta(hours=1), "PLC", datetime.now(), False)
        self.assertTrue(validate_event(future, None, datetime.now()))

    def test_stop_overlapping_manual_downtime_does_not_double_count(self):
        self.downtime(self.eq, T - timedelta(minutes=5), T + timedelta(minutes=30))
        result = self.proc.apply(event(self.eq, STOPPED, 0))
        self.assertIsNone(result.downtime_id)
        self.assertEqual(len(self.db.list_downtimes()), 1)
        self.assertIn("без двойного учёта", self.db.list_events()[0]["comment"])

    def test_legacy_open_stop_without_downtime(self):
        with self.db.transaction() as con:
            self.db.insert_event(con, self.eq, STOPPED, T, source_name="v8", comment="", downtime_id=None,
                                 received_at=T)
        result = self.proc.apply(event(self.eq, RUNNING, 30))
        downs = self.db.list_downtimes()
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0]["end_at"], "2026-10-20 10:30:00")
        self.assertEqual(result.closed_downtime_id, downs[0]["id"])


class SimulatorTests(DBTestCase):
    def run_shift(self, seed=7):
        eq = self.equipment("ДЕМО — тест", 1.2)
        sim = ShiftSimulator(eq, "ДЕМО — тест", date(2026, 10, 20), "1 смена", 1.2, seed=seed)
        proc = EventProcessor(self.db)
        states = []
        while not sim.finished:
            for e in sim.step():
                states.append(e.state)
                self.assertEqual(proc.apply(e).status, "applied")
        proc.close_session(eq, sim.end)
        return eq, sim, states

    def test_shift_is_consistent(self):
        eq, sim, states = self.run_shift()
        c = sim.counters
        self.assertEqual(c.minute, 480)
        self.assertEqual(c.run_min + c.stop_min, 480)
        self.assertEqual(c.reported_total, c.total)
        downtime = sum((parse_ts(d["end_at"]) - parse_ts(d["start_at"])).total_seconds() / 60
                       for d in self.db.list_downtimes())
        self.assertAlmostEqual(downtime, c.stop_min)
        self.assertTrue(all(d["end_at"] for d in self.db.list_downtimes()))
        self.assertIsNone(self.db.current_state(eq))
        record = sim.production_record()
        self.db.add_production(record, source="simulator")
        r = oee.calculate(record.planned_min, record.run_min, record.total_count, record.good_count, 1.2)
        self.assertLessEqual(r.performance, 1.0)
        self.assertEqual(self.db.list_production(Filters(source="generated"))[0]["source"], "simulator")

    def test_simulator_is_deterministic(self):
        _, a, states_a = self.run_shift(seed=11)
        b = ShiftSimulator(1, "ДЕМО — тест", date(2026, 10, 20), "1 смена", 1.2, seed=11)
        while not b.finished:
            b.step()
        self.assertEqual((a.counters.total, a.counters.stop_min), (b.counters.total, b.counters.stop_min))
        self.assertEqual(a.timeline, b.timeline)

    def test_rollback_of_session(self):
        eq = self.equipment("ДЕМО — откат", 1.0)
        sim = ShiftSimulator(eq, "ДЕМО — откат", date(2026, 10, 20), "2 смена", 1.0, seed=3, stop_probability=0.2)
        proc = EventProcessor(self.db)
        events, downs = [], []
        for _ in range(60):
            for e in sim.step():
                result = proc.apply(e)
                events.append(result.event_id)
                if result.downtime_id:
                    downs.append(result.downtime_id)
        self.assertTrue(downs)
        self.db.delete_rows(event_ids=events, downtime_ids=downs, production_ids=[])
        self.assertEqual(self.db.list_events(), [])
        self.assertEqual(self.db.list_downtimes(), [])


if __name__ == "__main__":
    unittest.main()
