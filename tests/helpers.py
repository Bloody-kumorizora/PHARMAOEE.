"""Общие инструменты тестов: временная база, создание записей, база в формате v8.

Тесты работают только с временными каталогами и не трогают пользовательские данные.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from pharmaoee_app.db import Database
from pharmaoee_app.validation import DowntimeInput, EquipmentInput, ProductionInput


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pharmaoee_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class DBTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.backups = self.tmp / "backups"
        self.db = Database(self.tmp / "test.db")
        self.db.initialize(self.backups)

    def equipment(self, name="Линия 1", cycle: float | None = 1.0) -> int:
        return self.db.add_equipment(EquipmentInput(name, "Участок", cycle))

    def production(self, eq: int, day: date, shift="1 смена", planned=480, run=420, total=1000, good=990,
                   cycle=1.2, source="manual") -> int:
        return self.db.add_production(ProductionInput(eq, day, shift, planned, run, total, good, cycle), source)

    def downtime(self, eq: int, start: datetime, end: datetime | None, reason="Переналадка", day: date | None = None,
                 shift="1 смена", source="manual") -> int:
        return self.db.add_downtime(DowntimeInput(eq, day or start.date(), shift, start, end, reason), source)


V8_SCHEMA = [
    """CREATE TABLE production (id INTEGER PRIMARY KEY AUTOINCREMENT, work_date TEXT NOT NULL, shift TEXT NOT NULL,
        line_name TEXT NOT NULL, planned_min REAL NOT NULL, run_min REAL NOT NULL, total_count INTEGER NOT NULL,
        good_count INTEGER NOT NULL, ideal_cycle_sec REAL NOT NULL, note TEXT DEFAULT '')""",
    """CREATE TABLE downtimes (id INTEGER PRIMARY KEY AUTOINCREMENT, work_date TEXT NOT NULL, shift TEXT NOT NULL,
        line_name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, duration_min REAL NOT NULL,
        reason TEXT NOT NULL, comment TEXT DEFAULT '')""",
    """CREATE TABLE sensor_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_start TEXT NOT NULL,
        event_end TEXT DEFAULT '', line_name TEXT NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL,
        reason TEXT DEFAULT 'Не классифицировано', comment TEXT DEFAULT '', downtime_id INTEGER DEFAULT NULL)""",
]


def make_v8_db(path: Path) -> Path:
    """База в формате PharmaOEE v8 с типичными записями (ручными, демо и симулятора)."""
    con = sqlite3.connect(path)
    with con:
        for statement in V8_SCHEMA:
            con.execute(statement)
        con.execute("INSERT INTO production VALUES (1,'2026-10-01','1 смена','Линия фасовки 1',480,420,18000,17640,1.2,'')")
        con.execute("INSERT INTO production VALUES (2,'2026-10-02','1 смена','ДЕМО — линия фасовки',480,420,18000,"
                    "17640,1.2,'ИСКУССТВЕННЫЕ ДАННЫЕ ДЛЯ ДЕМОНСТРАЦИИ')")
        con.execute("INSERT INTO production VALUES (3,'2026-10-01','2 смена','  Линия   фасовки 1 ',480,400,15000,14900,1.2,'')")
        con.execute("INSERT INTO downtimes VALUES (1,'2026-10-01','1 смена','Линия фасовки 1','10:00','10:15',15,"
                    "'Переналадка','вручную')")
        con.execute("INSERT INTO downtimes VALUES (2,'2026-10-02','1 смена','ДЕМО — линия фасовки','09:10','09:25',15,"
                    "'Ожидание материала','Демо-запись, не факт производства')")
        # Событие симулятора, перешедшее через полночь, с ручной классификацией.
        con.execute("INSERT INTO downtimes VALUES (3,'2026-10-03','1 смена','Линия фасовки 1','23:50','00:20',30.5,"
                    "'Механическая неисправность','Проверка')")
        con.execute("INSERT INTO sensor_events VALUES (1,'2026-10-03 23:50:00','2026-10-04 00:20:30','Линия фасовки 1',"
                    "'RUNNING','Симулятор сигнала','Механическая неисправность','Проверка',3)")
        con.execute("INSERT INTO sensor_events VALUES (2,'2026-10-05 08:00:00','','Линия фасовки 1','STOPPED',"
                    "'Симулятор сигнала','Не классифицировано','Демонстрационный сигнал STOPPED',NULL)")
    con.close()
    return path
