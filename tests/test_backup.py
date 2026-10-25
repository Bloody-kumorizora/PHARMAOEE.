"""Резервные копии: создание, проверка, восстановление, ошибки без потери текущей базы."""
import sqlite3
import unittest
from datetime import date
from unittest import mock

from pharmaoee_app import backup
from pharmaoee_app.backup import BackupError
from pharmaoee_app.db import SCHEMA_VERSION

from tests.helpers import DBTestCase, make_v8_db


class BackupTests(DBTestCase):
    def setUp(self):
        super().setUp()
        self.eq = self.equipment()
        self.production(self.eq, date(2026, 10, 1))

    def test_create_and_inspect(self):
        path = backup.create_backup(self.db.path, self.tmp / "copy.db")
        info = backup.inspect_backup(path)
        self.assertEqual(info.kind, "v9")
        self.assertEqual(info.counts["production"], 1)
        self.assertFalse((self.tmp / "copy.db.part").exists())

    def test_backup_over_working_db_refused(self):
        with self.assertRaises(BackupError):
            backup.create_backup(self.db.path, self.db.path)

    def test_backup_of_missing_db(self):
        with self.assertRaises(BackupError):
            backup.create_backup(self.tmp / "nope.db", self.tmp / "copy.db")

    def test_restore_success_and_safety_copy(self):
        snapshot = backup.create_backup(self.db.path, self.tmp / "copy.db")
        self.production(self.eq, date(2026, 10, 2))
        self.assertEqual(self.db.count_production(), 2)
        result = backup.restore_backup(self.db, snapshot, self.backups)
        self.assertEqual(self.db.count_production(), 1)
        self.assertTrue(result.safety_backup.exists())
        self.assertEqual(backup.inspect_backup(result.safety_backup).counts["production"], 2)
        self.assertFalse(self.db.path.with_name(self.db.path.name + ".restore-tmp").exists())

    def test_invalid_files_are_rejected_and_current_db_kept(self):
        text = self.tmp / "notes.db"
        text.write_text("не база данных", encoding="utf-8")
        empty = self.tmp / "empty.db"
        empty.write_bytes(b"")
        foreign = self.tmp / "foreign.db"
        con = sqlite3.connect(foreign)
        con.execute("CREATE TABLE customers (id)")
        con.commit()
        con.close()
        future = self.tmp / "future.db"
        backup.create_backup(self.db.path, future)
        con = sqlite3.connect(future)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        con.commit()
        con.close()
        for bad in (text, empty, foreign, future, self.tmp / "missing.db"):
            with self.subTest(bad=bad.name), self.assertRaises(BackupError):
                backup.restore_backup(self.db, bad, self.backups)
        self.assertEqual(self.db.count_production(), 1)
        self.assertEqual(self.db.quick_check(), "ok")

    def test_corrupted_copy_rejected(self):
        good = backup.create_backup(self.db.path, self.tmp / "copy.db")
        data = bytearray(good.read_bytes())
        data[100:4096] = b"\xff" * (4096 - 100)
        broken = self.tmp / "broken.db"
        broken.write_bytes(bytes(data))
        with self.assertRaises(BackupError):
            backup.inspect_backup(broken)
        with self.assertRaises(BackupError):
            backup.restore_backup(self.db, broken, self.backups)
        self.assertEqual(self.db.count_production(), 1)

    def test_failure_during_final_copy_keeps_current_data(self):
        snapshot = backup.create_backup(self.db.path, self.tmp / "copy.db")
        self.production(self.eq, date(2026, 10, 2))
        with mock.patch("pharmaoee_app.backup._copy_into", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(BackupError):
                backup.restore_backup(self.db, snapshot, self.backups)
        self.assertEqual(self.db.count_production(), 2)
        self.assertEqual(self.db.quick_check(), "ok")

    def test_restore_v8_database_is_migrated(self):
        v8 = make_v8_db(self.tmp / "old_v8.db")
        info = backup.inspect_backup(v8)
        self.assertEqual(info.kind, "v8")
        backup.restore_backup(self.db, v8, self.backups)
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)
        self.assertEqual(self.db.count_production(), 3)
        # Исходный файл v8 не изменён.
        self.assertEqual(backup.inspect_backup(v8).kind, "v8")

    def test_list_backups_newest_first(self):
        a = backup.create_backup(self.db.path, self.backups / "a.db")
        b = backup.create_backup(self.db.path, self.backups / "b.db")
        import os
        os.utime(a, (1_000_000, 1_000_000))
        self.assertEqual(backup.list_backups(self.backups)[0], b)


if __name__ == "__main__":
    unittest.main()
