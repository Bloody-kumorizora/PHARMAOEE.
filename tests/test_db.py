"""SQLite: создание, CRUD, транзакции, ограничения, фильтры дат, журнал изменений."""
import sqlite3
import unittest
from datetime import date, datetime, timedelta

from pharmaoee_app.db import (SCHEMA_VERSION, ConflictError, Database, Filters, MigrationError, NotFoundError)
from pharmaoee_app.validation import EquipmentInput, ProductionInput

from tests.helpers import DBTestCase, TempDirTestCase


class CreationTests(TempDirTestCase):
    def test_new_database_has_schema_and_reasons(self):
        db = Database(self.tmp / "new.db")
        result = db.initialize(self.tmp / "b")
        self.assertTrue(result.created)
        self.assertEqual(db.schema_version(), SCHEMA_VERSION)
        reasons = [r["name"] for r in db.list_reasons()]
        self.assertEqual(reasons[0], "Не классифицировано")
        self.assertIn("Переналадка", reasons)
        self.assertEqual(db.quick_check(), "ok")

    def test_initialize_is_idempotent(self):
        db = Database(self.tmp / "new.db")
        db.initialize()
        again = db.initialize()
        self.assertFalse(again.created)
        self.assertIsNone(again.migrated_from)

    def test_connect_does_not_create_missing_file(self):
        db = Database(self.tmp / "missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            with db.connect() as con:
                con.execute("SELECT 1")
        self.assertFalse((self.tmp / "missing.db").exists())

    def test_newer_schema_is_refused_without_changes(self):
        path = self.tmp / "future.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE x (a)")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        con.commit()
        con.close()
        with self.assertRaises(MigrationError):
            Database(path).initialize()

    def test_unknown_database_is_refused(self):
        path = self.tmp / "other.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE customers (id)")
        con.commit()
        con.close()
        with self.assertRaises(MigrationError):
            Database(path).initialize()
        con = sqlite3.connect(path)
        self.assertEqual([r[0] for r in con.execute("SELECT name FROM sqlite_master")], ["customers"])
        con.close()

    def test_path_with_cyrillic_spaces_and_special_characters(self):
        folder = self.tmp / "Пользователь Иван #1 & 50%"
        db = Database(folder / "pharmaoee.db")
        db.initialize(folder / "backups")
        eq = db.add_equipment(EquipmentInput("Линия"))
        db.add_production(ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))
        self.assertEqual(db.count_production(), 1)
        self.assertEqual(db.quick_check(), "ok")

    def test_not_a_database_file(self):
        path = self.tmp / "text.db"
        path.write_text("hello", encoding="utf-8")
        with self.assertRaises(sqlite3.DatabaseError):
            Database(path).initialize()


class EquipmentTests(DBTestCase):
    def test_add_update_and_case_insensitive_duplicate(self):
        eq = self.equipment("Линия фасовки")
        with self.assertRaises(ConflictError):
            self.db.add_equipment(EquipmentInput("линия ФАСОВКИ"))
        self.db.update_equipment(eq, EquipmentInput("Линия фасовки 2", "Цех", 2.0, "", False))
        row = self.db.get_equipment(eq)
        self.assertEqual(row["name"], "Линия фасовки 2")
        self.assertEqual(row["is_active"], 0)

    def test_delete_referenced_equipment_is_blocked(self):
        eq = self.equipment()
        self.production(eq, date(2026, 10, 1))
        with self.assertRaises(ConflictError):
            self.db.delete_equipment(eq)
        free = self.equipment("Свободная")
        self.db.delete_equipment(free)
        with self.assertRaises(NotFoundError):
            self.db.get_equipment(free)

    def test_foreign_key_enforced(self):
        with self.assertRaises(NotFoundError):
            self.db.add_production(ProductionInput(999, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as con:
                self.db.insert_production(con, ProductionInput(999, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))


class ProductionTests(DBTestCase):
    def test_crud_cycle(self):
        eq = self.equipment()
        rid = self.production(eq, date(2026, 10, 1))
        row = self.db.get_production(rid)
        self.assertEqual(row["equipment_name"], "Линия 1")
        self.assertEqual(row["source"], "manual")
        self.db.update_production(rid, ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 900, 890, 1.2, "испр."))
        self.assertEqual(self.db.get_production(rid)["run_min"], 400)
        self.db.delete_production(rid)
        with self.assertRaises(NotFoundError):
            self.db.get_production(rid)
        with self.assertRaises(NotFoundError):
            self.db.delete_production(rid)

    def test_duplicate_shift_record_blocked(self):
        eq = self.equipment()
        self.production(eq, date(2026, 10, 1))
        with self.assertRaises(ConflictError):
            self.production(eq, date(2026, 10, 1))
        self.production(eq, date(2026, 10, 1), shift="2 смена")
        other = self.production(eq, date(2026, 10, 2))
        with self.assertRaises(ConflictError):
            self.db.update_production(other, ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))

    def test_transaction_rolls_back_on_error(self):
        eq = self.equipment()
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as con:
                self.db.insert_production(con, ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 400, 1, 1, 1))
                raise RuntimeError("сбой посреди операции")
        self.assertEqual(self.db.count_production(), 0)

    def test_check_constraints(self):
        eq = self.equipment()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as con:
                con.execute("""INSERT INTO production (work_date, shift, equipment_id, planned_min, run_min,
                    total_count, good_count, ideal_cycle_sec, created_at, updated_at)
                    VALUES ('2026-10-01', '1 смена', ?, 480, -1, 0, 0, 1, '', '')""", (eq,))

    def test_date_filter_includes_both_bounds_and_selected_day(self):
        eq = self.equipment()
        for day in (date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)):
            self.production(eq, day)
        rows = self.db.list_production(Filters(date(2026, 10, 1), date(2026, 10, 2)))
        self.assertEqual(sorted(r["work_date"] for r in rows), ["2026-10-01", "2026-10-02"])
        single = self.db.list_production(Filters(date(2026, 10, 3), date(2026, 10, 3)))
        self.assertEqual([r["work_date"] for r in single], ["2026-10-03"])
        self.assertEqual(self.db.list_production(Filters(date(2026, 10, 4), date(2026, 10, 4))), [])

    def test_equipment_shift_and_source_filters(self):
        a, b = self.equipment("A"), self.equipment("B")
        self.production(a, date(2026, 10, 1))
        self.production(a, date(2026, 10, 1), shift="2 смена", source="demo")
        self.production(b, date(2026, 10, 1))
        self.assertEqual(len(self.db.list_production(Filters(equipment_id=a))), 2)
        self.assertEqual(len(self.db.list_production(Filters(shift="2 смена"))), 1)
        self.assertEqual(len(self.db.list_production(Filters(source="manual"))), 2)
        self.assertEqual(len(self.db.list_production(Filters(source="generated"))), 1)

    def test_change_log_records_manual_changes(self):
        eq = self.equipment()
        rid = self.production(eq, date(2026, 10, 1))
        self.db.update_production(rid, ProductionInput(eq, date(2026, 10, 1), "1 смена", 480, 410, 1000, 990, 1.2))
        self.db.delete_production(rid)
        actions = [(c["action"], c["entity"]) for c in self.db.list_changes()]
        self.assertIn(("update", "production"), actions)
        self.assertIn(("delete", "production"), actions)
        update = next(c for c in self.db.list_changes() if c["action"] == "update" and c["entity"] == "production")
        self.assertIn("run_min", update["details"])


class DowntimeTests(DBTestCase):
    def test_overlap_blocked_and_touching_allowed(self):
        eq = self.equipment()
        t = datetime(2026, 10, 1, 10, 0)
        self.downtime(eq, t, t + timedelta(minutes=30))
        with self.assertRaises(ConflictError):
            self.downtime(eq, t + timedelta(minutes=10), t + timedelta(minutes=40))
        self.downtime(eq, t + timedelta(minutes=30), t + timedelta(minutes=40))
        other = self.equipment("Другая")
        self.downtime(other, t, t + timedelta(minutes=30))

    def test_open_downtime_blocks_later_ones_and_can_be_closed(self):
        eq = self.equipment()
        t = datetime(2026, 10, 1, 10, 0)
        open_id = self.downtime(eq, t, None)
        with self.assertRaises(ConflictError):
            self.downtime(eq, t + timedelta(hours=2), t + timedelta(hours=3))
        with self.assertRaises(ConflictError):
            self.db.close_downtime(open_id, t - timedelta(minutes=1))
        self.db.close_downtime(open_id, t + timedelta(minutes=25))
        self.assertEqual(self.db.get_downtime(open_id)["end_at"], "2026-10-01 10:25:00")
        with self.assertRaises(ConflictError):
            self.db.close_downtime(open_id, t + timedelta(minutes=30))

    def test_midnight_downtime_and_shift_minutes(self):
        eq = self.equipment()
        start = datetime(2026, 10, 1, 23, 50)
        self.downtime(eq, start, start + timedelta(minutes=30), day=date(2026, 10, 1), shift="3 смена")
        minutes, open_count = self.db.shift_downtime_minutes(eq, date(2026, 10, 1), "3 смена")
        self.assertEqual((minutes, open_count), (30, 0))

    def test_rename_reason_updates_downtimes(self):
        eq = self.equipment()
        t = datetime(2026, 10, 1, 10, 0)
        did = self.downtime(eq, t, t + timedelta(minutes=5), reason="Переналадка")
        reason_id = next(r["id"] for r in self.db.list_reasons() if r["name"] == "Переналадка")
        self.assertEqual(self.db.rename_reason(reason_id, "Смена формата"), 1)
        self.assertEqual(self.db.get_downtime(did)["reason"], "Смена формата")
        system = next(r["id"] for r in self.db.list_reasons() if r["is_system"])
        with self.assertRaises(ConflictError):
            self.db.rename_reason(system, "Другое")
        with self.assertRaises(ConflictError):
            self.db.set_reason_active(system, False)
        with self.assertRaises(ConflictError):
            self.db.add_reason("смена ФОРМАТА")

    def test_delete_generated_never_touches_manual(self):
        eq = self.equipment()
        self.production(eq, date(2026, 10, 1))
        self.production(eq, date(2026, 10, 2), source="demo")
        t = datetime(2026, 10, 2, 9, 0)
        self.downtime(eq, t, t + timedelta(minutes=5), source="demo")
        self.downtime(eq, t + timedelta(hours=1), t + timedelta(hours=2))
        counts = self.db.delete_generated("demo")
        self.assertEqual((counts["production"], counts["downtimes"]), (1, 1))
        self.assertEqual(self.db.count_production(), 1)
        self.assertEqual(len(self.db.list_downtimes()), 1)
        with self.assertRaises(ValueError):
            self.db.delete_generated("manual")


if __name__ == "__main__":
    unittest.main()
