"""Миграция базы PharmaOEE v8 в схему v9 без потери данных."""
import sqlite3
import unittest

from pharmaoee_app.db import SCHEMA_VERSION, Database, MigrationError, parse_ts

from tests.helpers import TempDirTestCase, make_v8_db


class V8MigrationTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.path = make_v8_db(self.tmp / "v8.db")
        self.db = Database(self.path)

    def test_migration_preserves_records_and_creates_backup(self):
        result = self.db.initialize(self.tmp / "backups")
        self.assertEqual(result.migrated_from, "v8")
        self.assertTrue(result.backup_path.exists())
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)
        prod = {r["id"]: r for r in self.db.list_production()}
        self.assertEqual(set(prod), {1, 2, 3})
        self.assertEqual(prod[1]["source"], "manual")
        self.assertEqual(prod[2]["source"], "demo")
        # Имена с лишними пробелами объединяются в одно оборудование.
        self.assertEqual(prod[1]["equipment_id"], prod[3]["equipment_id"])
        equipment = {e["name"]: e for e in self.db.list_equipment()}
        self.assertEqual(set(equipment), {"Линия фасовки 1", "ДЕМО — линия фасовки"})
        self.assertEqual(equipment["ДЕМО — линия фасовки"]["is_demo"], 1)

    def test_downtimes_and_events_converted(self):
        self.db.initialize(self.tmp / "backups")
        downs = {d["id"]: d for d in self.db.list_downtimes()}
        self.assertEqual(downs[1]["start_at"], "2026-10-01 10:00:00")
        self.assertEqual(downs[1]["end_at"], "2026-10-01 10:15:00")
        self.assertEqual(downs[1]["source"], "manual")
        self.assertEqual(downs[2]["source"], "demo")
        # Простой симулятора через полночь берёт точные времена события.
        self.assertEqual(downs[3]["source"], "simulator")
        self.assertEqual(downs[3]["end_at"], "2026-10-04 00:20:30")
        self.assertGreater(parse_ts(downs[3]["end_at"]), parse_ts(downs[3]["start_at"]))
        events = {e["id"]: e for e in self.db.list_events()}
        self.assertEqual(events[1]["state"], "STOPPED")       # в v8 сохранено как RUNNING
        self.assertEqual(events[1]["downtime_id"], 3)
        self.assertIsNone(events[2]["end_at"])                # незавершённое событие осталось открытым
        reasons = [r["name"] for r in self.db.list_reasons()]
        self.assertIn("Механическая неисправность", reasons)

    def test_second_start_does_not_migrate_again(self):
        self.db.initialize(self.tmp / "backups")
        again = self.db.initialize(self.tmp / "backups")
        self.assertIsNone(again.migrated_from)
        self.assertEqual(self.db.count_production(), 3)

    def test_failed_migration_leaves_v8_database_untouched(self):
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO production VALUES (9,'не дата','1 смена','X',480,400,1,1,1,'')")
        con.commit()
        con.close()
        with self.assertRaises(MigrationError):
            self.db.initialize(self.tmp / "backups")
        con = sqlite3.connect(self.path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = con.execute("PRAGMA user_version").fetchone()[0]
        count = con.execute("SELECT COUNT(*) FROM production").fetchone()[0]
        con.close()
        self.assertIn("production", tables)
        self.assertNotIn("equipment", tables)
        self.assertEqual(version, 0)
        self.assertEqual(count, 4)


if __name__ == "__main__":
    unittest.main()
