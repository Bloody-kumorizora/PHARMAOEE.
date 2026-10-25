"""Доступ к SQLite: схема, миграции, репозитории и журнал изменений.

Все SQL-запросы приложения собраны в этом модуле. Каждый вызов открывает
короткое соединение и закрывает его; связанные изменения выполняются в одной
транзакции (BEGIN IMMEDIATE … COMMIT / ROLLBACK).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .config import (DEFAULT_REASONS, SHIFTS, SOURCE_DEMO, SOURCE_MANUAL, SOURCE_SIMULATOR,
                     UNCLASSIFIED_REASON)
from .validation import DowntimeInput, EquipmentInput, ProductionInput, normalize_name

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
FAR_FUTURE = "9999-12-31 23:59:59"
LEGACY_TABLES = ("production", "downtimes", "sensor_events")
SCHEMA_TABLES = ("equipment", "downtime_reasons", "production", "downtimes", "sensor_events",
                 "change_log", "app_meta")
V8_DEMO_NOTE = "ИСКУССТВЕННЫЕ ДАННЫЕ ДЛЯ ДЕМОНСТРАЦИИ"
V8_DEMO_DOWNTIME_COMMENT = "Демо-запись, не факт производства"

SCHEMA_SQL = [
    """CREATE TABLE equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
        area TEXT NOT NULL DEFAULT '',
        ideal_cycle_sec REAL CHECK (ideal_cycle_sec IS NULL OR ideal_cycle_sec > 0),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE downtime_reasons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE production (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_date TEXT NOT NULL CHECK (work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        shift TEXT NOT NULL,
        equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE RESTRICT,
        planned_min REAL NOT NULL CHECK (planned_min >= 0),
        run_min REAL NOT NULL CHECK (run_min >= 0),
        total_count INTEGER NOT NULL CHECK (total_count >= 0),
        good_count INTEGER NOT NULL CHECK (good_count >= 0),
        ideal_cycle_sec REAL NOT NULL CHECK (ideal_cycle_sec >= 0),
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'demo', 'simulator')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_production_date ON production(work_date)",
    "CREATE INDEX idx_production_equipment_date ON production(equipment_id, work_date, shift)",
    "CREATE INDEX idx_production_source ON production(source)",
    """CREATE TABLE downtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE RESTRICT,
        work_date TEXT NOT NULL CHECK (work_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        shift TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT,
        reason TEXT NOT NULL,
        comment TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'demo', 'simulator')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (end_at IS NULL OR end_at >= start_at)
    )""",
    "CREATE INDEX idx_downtimes_date ON downtimes(work_date)",
    "CREATE INDEX idx_downtimes_equipment_start ON downtimes(equipment_id, start_at)",
    "CREATE INDEX idx_downtimes_source ON downtimes(source)",
    """CREATE TABLE sensor_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE RESTRICT,
        state TEXT NOT NULL CHECK (state IN ('RUNNING', 'STOPPED', 'CONNECTION_LOST')),
        start_at TEXT NOT NULL,
        end_at TEXT,
        source TEXT NOT NULL DEFAULT 'simulator' CHECK (source IN ('manual', 'demo', 'simulator')),
        source_name TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        downtime_id INTEGER REFERENCES downtimes(id) ON DELETE SET NULL,
        received_at TEXT NOT NULL,
        CHECK (end_at IS NULL OR end_at >= start_at)
    )""",
    "CREATE INDEX idx_sensor_equipment_start ON sensor_events(equipment_id, start_at)",
    """CREATE TABLE change_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        changed_at TEXT NOT NULL,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id INTEGER,
        details TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]


# --- Ошибки ----------------------------------------------------------------------

class DatabaseError(Exception):
    """Ошибка работы с данными с понятным пользователю текстом."""


class NotFoundError(DatabaseError):
    pass


class ConflictError(DatabaseError):
    """Дубликат, пересечение или запрещённое удаление."""


class MigrationError(DatabaseError):
    pass


def describe_error(exc: BaseException) -> str:
    """Перевод системной ошибки SQLite/ОС в понятное сообщение."""
    if isinstance(exc, DatabaseError):
        return str(exc)
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "База данных занята другим процессом. Закройте другие копии PharmaOEE и повторите."
    if "disk is full" in text or "database or disk is full" in text:
        return "Недостаточно места на диске для записи базы данных."
    if "readonly" in text or "read-only" in text:
        return "Нет прав на запись в файл базы данных."
    if "not a database" in text or "malformed" in text or "corrupt" in text:
        return "Файл базы данных повреждён или не является базой SQLite."
    if "unable to open" in text:
        return "Не удалось открыть файл базы данных: проверьте путь и права доступа."
    if isinstance(exc, PermissionError):
        return "Нет прав доступа к файлу."
    if isinstance(exc, OSError):
        return f"Ошибка файловой системы: {exc.strerror or exc}"
    return f"Ошибка базы данных: {exc}"


# --- Вспомогательные функции -------------------------------------------------------

def ts(moment: datetime) -> str:
    return moment.strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT)


def now_ts() -> str:
    return ts(datetime.now())


@dataclass(frozen=True)
class Filters:
    """Единый набор фильтров для показателей, графиков, таблиц и отчётов.

    Период — по дате смены, обе границы включительно.
    """

    date_from: date | None = None
    date_to: date | None = None
    equipment_id: int | None = None
    shift: str | None = None
    source: str = "all"          # all / manual / generated

    def where(self, alias: str) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        if self.date_from:
            parts.append(f"{alias}.work_date >= ?")
            params.append(self.date_from.isoformat())
        if self.date_to:
            parts.append(f"{alias}.work_date <= ?")
            params.append(self.date_to.isoformat())
        if self.equipment_id is not None:
            parts.append(f"{alias}.equipment_id = ?")
            params.append(self.equipment_id)
        if self.shift:
            parts.append(f"{alias}.shift = ?")
            params.append(self.shift)
        if self.source == "manual":
            parts.append(f"{alias}.source = 'manual'")
        elif self.source == "generated":
            parts.append(f"{alias}.source IN ('demo', 'simulator')")
        return (" WHERE " + " AND ".join(parts)) if parts else "", params


@dataclass(frozen=True)
class InitResult:
    created: bool
    migrated_from: str | None
    backup_path: Path | None


def _dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _log_change(con: sqlite3.Connection, action: str, entity: str, entity_id: int | None,
                details: dict | str) -> None:
    text = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False, default=str)
    con.execute("INSERT INTO change_log (changed_at, action, entity, entity_id, details) VALUES (?,?,?,?,?)",
                (now_ts(), action, entity, entity_id, text))


def _diff(old: dict, new: dict) -> dict:
    return {key: [old.get(key), value] for key, value in new.items() if old.get(key) != value}


class Database:
    """Файл базы данных и операции над ним."""

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()

    # --- Соединения и транзакции ------------------------------------------------

    @contextmanager
    def connect(self, *, create: bool = False, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        """Короткое соединение. Режим rw не создаёт новый пустой файл, если базы нет."""
        mode = "ro" if readonly else ("rwc" if create else "rw")
        con = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=5.0,
                              isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA busy_timeout = 5000")
            yield con
        finally:
            con.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    # --- Создание и миграции ------------------------------------------------------

    def schema_version(self) -> int:
        with self.connect(readonly=True) as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    def initialize(self, backups_dir: Path | None = None) -> InitResult:
        """Создаёт новую базу или обновляет схему существующей.

        Перед миграцией старой базы создаётся резервная копия (если указан каталог).
        Миграция выполняется в одной транзакции: при ошибке база остаётся прежней.
        """
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(create=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version > SCHEMA_VERSION:
            raise MigrationError(f"База создана более новой версией PharmaOEE (схема {version}). "
                                 "Обновите программу; база не изменена.")
        if version == SCHEMA_VERSION:
            missing = [t for t in SCHEMA_TABLES if t not in tables]
            if missing:
                raise MigrationError("В базе отсутствуют таблицы: " + ", ".join(missing))
            return InitResult(created=False, migrated_from=None, backup_path=None)
        user_tables = {t for t in tables if not t.startswith("sqlite_")}
        if not user_tables:
            self._create_schema()
            log.info("Создана новая база данных (схема %s)", SCHEMA_VERSION)
            return InitResult(created=not existed, migrated_from=None, backup_path=None)
        if "production" in user_tables and "downtimes" in user_tables and "equipment" not in user_tables:
            backup_path = None
            if backups_dir is not None:
                from .backup import create_backup, timestamped_name
                backup_path = create_backup(self.path, Path(backups_dir) / timestamped_name("before_migration_v8"))
            self._migrate_from_v8()
            log.info("База v8 обновлена до схемы %s; копия до миграции: %s", SCHEMA_VERSION, backup_path)
            return InitResult(created=False, migrated_from="v8", backup_path=backup_path)
        raise MigrationError("Файл не похож на базу PharmaOEE: неизвестный набор таблиц. База не изменена.")

    def _create_schema(self) -> None:
        with self.transaction() as con:
            for statement in SCHEMA_SQL:
                con.execute(statement)
            self._seed_reasons(con, [])
            con.execute("INSERT INTO app_meta (key, value) VALUES ('created_at', ?)", (now_ts(),))
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _seed_reasons(con: sqlite3.Connection, extra: list[str]) -> None:
        names = list(DEFAULT_REASONS) + [r for r in extra if r and r not in DEFAULT_REASONS]
        con.execute("INSERT INTO downtime_reasons (name, is_system, sort_order) VALUES (?, 1, 0)",
                    (UNCLASSIFIED_REASON,))
        for order, name in enumerate(n for n in names if n != UNCLASSIFIED_REASON):
            con.execute("INSERT OR IGNORE INTO downtime_reasons (name, sort_order) VALUES (?, ?)",
                        (name, order + 1))

    def _migrate_from_v8(self) -> None:
        with self.connect() as con:
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("BEGIN IMMEDIATE")
            try:
                self._copy_v8_data(con)
                problems = con.execute("PRAGMA foreign_key_check").fetchall()
                if problems:
                    raise MigrationError("Нарушены связи между таблицами после миграции.")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            finally:
                con.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _copy_v8_data(con: sqlite3.Connection) -> None:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prod = [dict(r) for r in con.execute("SELECT * FROM production ORDER BY id")]
        down = [dict(r) for r in con.execute("SELECT * FROM downtimes ORDER BY id")]
        events = ([dict(r) for r in con.execute("SELECT * FROM sensor_events ORDER BY id")]
                  if "sensor_events" in tables else [])
        for table in LEGACY_TABLES:
            if table in tables:
                con.execute(f"ALTER TABLE {table} RENAME TO v8_{table}")
        for statement in SCHEMA_SQL:
            con.execute(statement)
        stamp = now_ts()

        equipment_ids: dict[str, int] = {}

        def equipment_id(raw_name) -> int:
            name = normalize_name(str(raw_name or "")) or "Без названия"
            key = name.casefold()
            if key not in equipment_ids:
                cur = con.execute("INSERT INTO equipment (name, is_demo, created_at) VALUES (?, ?, ?)",
                                  (name, int(name.upper().startswith("ДЕМО")), stamp))
                equipment_ids[key] = cur.lastrowid
            return equipment_ids[key]

        legacy_reasons = sorted({str(r.get("reason") or "").strip() for r in down + events} - {""})
        Database._seed_reasons(con, legacy_reasons)

        def valid_date(value, what: str, row_id) -> str:
            try:
                return date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise MigrationError(f"Не удалось перенести {what} ID {row_id}: некорректная дата «{value}».") from None

        for r in prod:
            note = str(r.get("note") or "")
            source = SOURCE_DEMO if note == V8_DEMO_NOTE else SOURCE_MANUAL
            con.execute("""INSERT INTO production (id, work_date, shift, equipment_id, planned_min, run_min,
                               total_count, good_count, ideal_cycle_sec, note, source, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["id"], valid_date(r["work_date"], "смену", r["id"]), r["shift"] or SHIFTS[0],
                         equipment_id(r["line_name"]), r["planned_min"] or 0, r["run_min"] or 0,
                         r["total_count"] or 0, r["good_count"] or 0, r["ideal_cycle_sec"] or 0,
                         note, source, stamp, stamp))

        linked = {e["downtime_id"]: e for e in events if e.get("downtime_id")}
        for r in down:
            work_date = valid_date(r["work_date"], "простой", r["id"])
            event = linked.get(r["id"])
            try:
                if event and event.get("event_start") and event.get("event_end"):
                    start = parse_ts(event["event_start"])
                    end = parse_ts(event["event_end"])
                else:
                    day = date.fromisoformat(work_date)
                    start = datetime.combine(day, datetime.strptime(r["start_time"], "%H:%M").time())
                    end = datetime.combine(day, datetime.strptime(r["end_time"], "%H:%M").time())
                    if end < start:      # событие симулятора, перешедшее через полночь
                        end += timedelta(days=1)
            except (TypeError, ValueError):
                raise MigrationError(f"Не удалось перенести простой ID {r['id']}: некорректное время.") from None
            comment = str(r.get("comment") or "")
            if event or comment.startswith("Источник: симулятор"):
                source = SOURCE_SIMULATOR
            elif comment == V8_DEMO_DOWNTIME_COMMENT:
                source = SOURCE_DEMO
            else:
                source = SOURCE_MANUAL
            con.execute("""INSERT INTO downtimes (id, equipment_id, work_date, shift, start_at, end_at, reason,
                               comment, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["id"], equipment_id(r["line_name"]), work_date, r["shift"] or SHIFTS[0], ts(start),
                         ts(end), str(r.get("reason") or UNCLASSIFIED_REASON), comment, source, stamp, stamp))

        for e in events:
            # В v8 закрытое событие остановки получало state='RUNNING'; восстанавливаем исходный тип.
            closed = bool(e.get("event_end"))
            state = e.get("state") or "STOPPED"
            reason = str(e.get("reason") or "")
            comment = str(e.get("comment") or "")
            if closed and state == "RUNNING":
                lost = reason == "Потеря связи" or comment.startswith("Демонстрационное событие: связь")
                state = "CONNECTION_LOST" if lost else "STOPPED"
            if state not in ("RUNNING", "STOPPED", "CONNECTION_LOST"):
                state = "STOPPED"
            if reason and reason != UNCLASSIFIED_REASON and not e.get("downtime_id"):
                comment = f"Причина (v8): {reason}. {comment}".strip()
            try:
                start = parse_ts(e["event_start"])
                end = parse_ts(e["event_end"]) if closed else None
            except (TypeError, ValueError):
                raise MigrationError(f"Не удалось перенести событие ID {e['id']}: некорректное время.") from None
            con.execute("""INSERT INTO sensor_events (id, equipment_id, state, start_at, end_at, source,
                               source_name, comment, downtime_id, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (e["id"], equipment_id(e["line_name"]), state, ts(start), ts(end) if end else None,
                         SOURCE_SIMULATOR, str(e.get("source") or "Симулятор сигнала"), comment,
                         e.get("downtime_id"), ts(start)))

        for table in LEGACY_TABLES:
            if table in tables:
                con.execute(f"DROP TABLE v8_{table}")
        con.execute("INSERT INTO app_meta (key, value) VALUES ('created_at', ?)", (stamp,))
        con.execute("INSERT INTO app_meta (key, value) VALUES ('migrated_from', 'v8')")
        _log_change(con, "migrate", "database", None,
                    {"from": "v8", "production": len(prod), "downtimes": len(down), "events": len(events)})
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- Состояние базы -------------------------------------------------------------

    def quick_check(self) -> str:
        with self.connect(readonly=True) as con:
            return con.execute("PRAGMA quick_check").fetchone()[0]

    def integrity_check(self) -> str:
        with self.connect(readonly=True) as con:
            rows = con.execute("PRAGMA integrity_check").fetchall()
        return "; ".join(r[0] for r in rows[:5])

    def stats(self) -> dict:
        with self.connect(readonly=True) as con:
            result = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("equipment", "production", "downtimes", "sensor_events", "change_log")}
            result["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
        result["size_bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return result

    def source_counts(self) -> dict[str, dict[str, int]]:
        counts = {s: {"production": 0, "downtimes": 0, "sensor_events": 0}
                  for s in (SOURCE_MANUAL, SOURCE_DEMO, SOURCE_SIMULATOR)}
        with self.connect(readonly=True) as con:
            for table in ("production", "downtimes", "sensor_events"):
                for row in con.execute(f"SELECT source, COUNT(*) AS n FROM {table} GROUP BY source"):
                    counts.setdefault(row["source"], {})[table] = row["n"]
        return counts

    def date_bounds(self) -> tuple[date | None, date | None]:
        with self.connect(readonly=True) as con:
            row = con.execute("""SELECT MIN(d) AS lo, MAX(d) AS hi FROM (
                                     SELECT work_date AS d FROM production UNION ALL
                                     SELECT work_date FROM downtimes)""").fetchone()
        if not row or not row["lo"]:
            return None, None
        return date.fromisoformat(row["lo"]), date.fromisoformat(row["hi"])

    def list_changes(self, limit: int = 500) -> list[dict]:
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,))]

    # --- Оборудование ---------------------------------------------------------------

    def list_equipment(self, *, active_only: bool = False) -> list[dict]:
        query = """SELECT e.*,
                       (SELECT COUNT(*) FROM production p WHERE p.equipment_id = e.id) AS production_count,
                       (SELECT COUNT(*) FROM downtimes d WHERE d.equipment_id = e.id) AS downtime_count
                   FROM equipment e"""
        if active_only:
            query += " WHERE e.is_active = 1"
        query += " ORDER BY e.is_demo, e.name"
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(query)]

    def get_equipment(self, equipment_id: int) -> dict:
        with self.connect(readonly=True) as con:
            row = con.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("Оборудование не найдено — возможно, оно было удалено.")
        return dict(row)

    @staticmethod
    def _check_equipment_name(con: sqlite3.Connection, name: str, exclude_id: int | None = None) -> None:
        key = name.casefold()
        for row in con.execute("SELECT id, name FROM equipment"):
            if row["id"] != exclude_id and row["name"].casefold() == key:
                raise ConflictError(f"Оборудование «{row['name']}» уже есть в справочнике.")

    @staticmethod
    def ensure_equipment(con: sqlite3.Connection, name: str, *, area: str = "",
                         ideal_cycle_sec: float | None = None, is_demo: bool = False) -> int:
        """ID оборудования по имени; создаёт запись, если её нет (для демо-данных)."""
        key = normalize_name(name).casefold()
        for row in con.execute("SELECT id, name FROM equipment"):
            if row["name"].casefold() == key:
                return row["id"]
        cur = con.execute("""INSERT INTO equipment (name, area, ideal_cycle_sec, is_demo, note, created_at)
                             VALUES (?,?,?,?,?,?)""",
                          (normalize_name(name), area, ideal_cycle_sec, int(is_demo),
                           "Создано генератором демо-данных" if is_demo else "", now_ts()))
        return cur.lastrowid

    def add_equipment(self, data: EquipmentInput, *, is_demo: bool = False) -> int:
        with self.transaction() as con:
            self._check_equipment_name(con, data.name)
            cur = con.execute("""INSERT INTO equipment (name, area, ideal_cycle_sec, is_active, is_demo, note,
                                     created_at) VALUES (?,?,?,?,?,?,?)""",
                              (data.name, data.area, data.ideal_cycle_sec, int(data.is_active), int(is_demo),
                               data.note, now_ts()))
            _log_change(con, "create", "equipment", cur.lastrowid, {"name": data.name})
            return cur.lastrowid

    def update_equipment(self, equipment_id: int, data: EquipmentInput) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone())
            if old is None:
                raise NotFoundError("Оборудование не найдено.")
            self._check_equipment_name(con, data.name, exclude_id=equipment_id)
            new = {"name": data.name, "area": data.area, "ideal_cycle_sec": data.ideal_cycle_sec,
                   "is_active": int(data.is_active), "note": data.note}
            con.execute("""UPDATE equipment SET name=?, area=?, ideal_cycle_sec=?, is_active=?, note=?
                           WHERE id=?""", (*new.values(), equipment_id))
            changes = _diff(old, new)
            if changes:
                _log_change(con, "update", "equipment", equipment_id, changes)

    def delete_equipment(self, equipment_id: int) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone())
            if old is None:
                raise NotFoundError("Оборудование не найдено.")
            used = sum(con.execute(f"SELECT COUNT(*) FROM {t} WHERE equipment_id = ?", (equipment_id,)).fetchone()[0]
                       for t in ("production", "downtimes", "sensor_events"))
            if used:
                raise ConflictError(f"Оборудование используется в записях ({used}). Удаление запрещено, "
                                    "чтобы не потерять историю: снимите отметку «Используется» вместо удаления.")
            con.execute("DELETE FROM equipment WHERE id = ?", (equipment_id,))
            _log_change(con, "delete", "equipment", equipment_id, {"name": old["name"]})

    # --- Причины простоев -----------------------------------------------------------

    def list_reasons(self, *, active_only: bool = False) -> list[dict]:
        query = """SELECT r.*, (SELECT COUNT(*) FROM downtimes d WHERE d.reason = r.name) AS usage
                   FROM downtime_reasons r"""
        if active_only:
            query += " WHERE r.is_active = 1"
        query += " ORDER BY r.is_system DESC, r.sort_order, r.name"
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(query)]

    @staticmethod
    def _check_reason_name(con: sqlite3.Connection, name: str, exclude_id: int | None = None) -> None:
        for row in con.execute("SELECT id, name FROM downtime_reasons"):
            if row["id"] != exclude_id and row["name"].casefold() == name.casefold():
                raise ConflictError(f"Причина «{row['name']}» уже есть в справочнике.")

    def add_reason(self, name: str) -> int:
        name = normalize_name(name)
        if not name:
            raise DatabaseError("Введите название причины.")
        with self.transaction() as con:
            self._check_reason_name(con, name)
            order = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM downtime_reasons").fetchone()[0]
            cur = con.execute("INSERT INTO downtime_reasons (name, sort_order) VALUES (?, ?)", (name, order))
            _log_change(con, "create", "reason", cur.lastrowid, {"name": name})
            return cur.lastrowid

    def rename_reason(self, reason_id: int, new_name: str) -> int:
        """Переименование обновляет и уже сохранённые простои (в одной транзакции)."""
        new_name = normalize_name(new_name)
        if not new_name:
            raise DatabaseError("Введите название причины.")
        with self.transaction() as con:
            row = con.execute("SELECT * FROM downtime_reasons WHERE id = ?", (reason_id,)).fetchone()
            if row is None:
                raise NotFoundError("Причина не найдена.")
            if row["is_system"]:
                raise ConflictError("Системную причину «Не классифицировано» переименовать нельзя.")
            self._check_reason_name(con, new_name, exclude_id=reason_id)
            con.execute("UPDATE downtime_reasons SET name = ? WHERE id = ?", (new_name, reason_id))
            updated = con.execute("UPDATE downtimes SET reason = ?, updated_at = ? WHERE reason = ?",
                                  (new_name, now_ts(), row["name"])).rowcount
            _log_change(con, "update", "reason", reason_id,
                        {"name": [row["name"], new_name], "downtimes_updated": updated})
            return updated

    def set_reason_active(self, reason_id: int, active: bool) -> None:
        with self.transaction() as con:
            row = con.execute("SELECT * FROM downtime_reasons WHERE id = ?", (reason_id,)).fetchone()
            if row is None:
                raise NotFoundError("Причина не найдена.")
            if row["is_system"] and not active:
                raise ConflictError("Системную причину отключить нельзя.")
            con.execute("UPDATE downtime_reasons SET is_active = ? WHERE id = ?", (int(active), reason_id))
            _log_change(con, "update", "reason", reason_id, {"is_active": [row["is_active"], int(active)]})

    # --- Производственные записи ------------------------------------------------------

    _PRODUCTION_SELECT = """SELECT p.*, e.name AS equipment_name, e.is_demo AS equipment_is_demo
                            FROM production p JOIN equipment e ON e.id = p.equipment_id"""

    def list_production(self, filters: Filters = Filters(), limit: int | None = None) -> list[dict]:
        clause, params = filters.where("p")
        query = self._PRODUCTION_SELECT + clause + " ORDER BY p.work_date DESC, p.shift, e.name, p.id"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(query, params)]

    def count_production(self, filters: Filters = Filters()) -> int:
        clause, params = filters.where("p")
        with self.connect(readonly=True) as con:
            return con.execute("SELECT COUNT(*) FROM production p" + clause, params).fetchone()[0]

    def get_production(self, record_id: int) -> dict:
        with self.connect(readonly=True) as con:
            row = con.execute(self._PRODUCTION_SELECT + " WHERE p.id = ?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("Запись не найдена — возможно, она была удалена.")
        return dict(row)

    @staticmethod
    def find_duplicate_production(con: sqlite3.Connection, work_date: date, shift: str, equipment_id: int,
                                  exclude_id: int | None = None) -> int | None:
        row = con.execute("""SELECT id FROM production WHERE work_date = ? AND shift = ? AND equipment_id = ?
                             AND id IS NOT ? LIMIT 1""",
                          (work_date.isoformat(), shift, equipment_id, exclude_id)).fetchone()
        return row["id"] if row else None

    @staticmethod
    def insert_production(con: sqlite3.Connection, data: ProductionInput, source: str = SOURCE_MANUAL) -> int:
        stamp = now_ts()
        cur = con.execute("""INSERT INTO production (work_date, shift, equipment_id, planned_min, run_min,
                                 total_count, good_count, ideal_cycle_sec, note, source, created_at, updated_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (data.work_date.isoformat(), data.shift, data.equipment_id, data.planned_min,
                           data.run_min, data.total_count, data.good_count, data.ideal_cycle_sec, data.note,
                           source, stamp, stamp))
        return cur.lastrowid

    def _require_equipment(self, con: sqlite3.Connection, equipment_id: int) -> dict:
        row = con.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("Выбранное оборудование не найдено. Обновите список.")
        return dict(row)

    def add_production(self, data: ProductionInput, source: str = SOURCE_MANUAL) -> int:
        """Сохраняет запись смены; запрещает дубликат (дата + смена + оборудование)."""
        with self.transaction() as con:
            equipment = self._require_equipment(con, data.equipment_id)
            duplicate = self.find_duplicate_production(con, data.work_date, data.shift, data.equipment_id)
            if duplicate:
                raise ConflictError(f"Запись за {data.work_date:%d.%m.%Y}, {data.shift}, «{equipment['name']}» "
                                    f"уже существует (ID {duplicate}). Откройте её для изменения.")
            record_id = self.insert_production(con, data, source)
            _log_change(con, "create", "production", record_id,
                        {"date": data.work_date, "shift": data.shift, "equipment": equipment["name"]})
            return record_id

    def update_production(self, record_id: int, data: ProductionInput) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM production WHERE id = ?", (record_id,)).fetchone())
            if old is None:
                raise NotFoundError("Запись не найдена — возможно, она была удалена.")
            self._require_equipment(con, data.equipment_id)
            duplicate = self.find_duplicate_production(con, data.work_date, data.shift, data.equipment_id,
                                                       exclude_id=record_id)
            if duplicate:
                raise ConflictError(f"На эту дату, смену и оборудование уже есть запись ID {duplicate}.")
            new = {"work_date": data.work_date.isoformat(), "shift": data.shift,
                   "equipment_id": data.equipment_id, "planned_min": data.planned_min, "run_min": data.run_min,
                   "total_count": data.total_count, "good_count": data.good_count,
                   "ideal_cycle_sec": data.ideal_cycle_sec, "note": data.note}
            changes = _diff(old, new)
            if not changes:
                return
            con.execute("""UPDATE production SET work_date=?, shift=?, equipment_id=?, planned_min=?, run_min=?,
                               total_count=?, good_count=?, ideal_cycle_sec=?, note=?, updated_at=? WHERE id=?""",
                        (*new.values(), now_ts(), record_id))
            _log_change(con, "update", "production", record_id, changes)

    def delete_production(self, record_id: int) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM production WHERE id = ?", (record_id,)).fetchone())
            if old is None:
                raise NotFoundError("Запись уже удалена.")
            con.execute("DELETE FROM production WHERE id = ?", (record_id,))
            _log_change(con, "delete", "production", record_id, old)

    # --- Простои -------------------------------------------------------------------------

    _DOWNTIME_SELECT = """SELECT d.*, e.name AS equipment_name, e.is_demo AS equipment_is_demo
                          FROM downtimes d JOIN equipment e ON e.id = d.equipment_id"""

    def list_downtimes(self, filters: Filters = Filters(), limit: int | None = None) -> list[dict]:
        clause, params = filters.where("d")
        query = self._DOWNTIME_SELECT + clause + " ORDER BY d.start_at DESC, d.id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(query, params)]

    def get_downtime(self, downtime_id: int) -> dict:
        with self.connect(readonly=True) as con:
            row = con.execute(self._DOWNTIME_SELECT + " WHERE d.id = ?", (downtime_id,)).fetchone()
        if row is None:
            raise NotFoundError("Простой не найден — возможно, он был удалён.")
        return dict(row)

    def open_downtimes(self) -> list[dict]:
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(self._DOWNTIME_SELECT + " WHERE d.end_at IS NULL"
                                                 " ORDER BY d.start_at")]

    @staticmethod
    def find_overlapping_downtimes(con: sqlite3.Connection, equipment_id: int, start: datetime,
                                   end: datetime | None, exclude_id: int | None = None) -> list[dict]:
        """Простои того же оборудования, пересекающиеся с [start, end). Открытый простой
        считается продолжающимся бесконечно."""
        rows = con.execute("""SELECT id, start_at, end_at, reason FROM downtimes
                              WHERE equipment_id = ? AND id IS NOT ?
                                AND start_at < ? AND COALESCE(end_at, ?) > ?
                              ORDER BY start_at""",
                           (equipment_id, exclude_id, ts(end) if end else FAR_FUTURE, FAR_FUTURE,
                            ts(start))).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _overlap_message(conflicts: list[dict]) -> str:
        first = conflicts[0]
        end = first["end_at"][11:16] if first["end_at"] else "продолжается"
        return (f"Интервал пересекается с простоем ID {first['id']} "
                f"({first['start_at'][8:10]}.{first['start_at'][5:7]} {first['start_at'][11:16]}–{end}, "
                f"«{first['reason']}»). Одно и то же время нельзя учитывать дважды: "
                "измените интервал или существующую запись.")

    @staticmethod
    def insert_downtime(con: sqlite3.Connection, data: DowntimeInput, source: str = SOURCE_MANUAL) -> int:
        stamp = now_ts()
        cur = con.execute("""INSERT INTO downtimes (equipment_id, work_date, shift, start_at, end_at, reason,
                                 comment, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (data.equipment_id, data.work_date.isoformat(), data.shift, ts(data.start_at),
                           ts(data.end_at) if data.end_at else None, data.reason, data.comment, source,
                           stamp, stamp))
        return cur.lastrowid

    def add_downtime(self, data: DowntimeInput, source: str = SOURCE_MANUAL) -> int:
        with self.transaction() as con:
            equipment = self._require_equipment(con, data.equipment_id)
            conflicts = self.find_overlapping_downtimes(con, data.equipment_id, data.start_at, data.end_at)
            if conflicts:
                raise ConflictError(self._overlap_message(conflicts))
            downtime_id = self.insert_downtime(con, data, source)
            _log_change(con, "create", "downtime", downtime_id,
                        {"equipment": equipment["name"], "start": data.start_at, "end": data.end_at,
                         "reason": data.reason})
            return downtime_id

    def update_downtime(self, downtime_id: int, data: DowntimeInput) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM downtimes WHERE id = ?", (downtime_id,)).fetchone())
            if old is None:
                raise NotFoundError("Простой не найден — возможно, он был удалён.")
            self._require_equipment(con, data.equipment_id)
            conflicts = self.find_overlapping_downtimes(con, data.equipment_id, data.start_at, data.end_at,
                                                        exclude_id=downtime_id)
            if conflicts:
                raise ConflictError(self._overlap_message(conflicts))
            new = {"equipment_id": data.equipment_id, "work_date": data.work_date.isoformat(),
                   "shift": data.shift, "start_at": ts(data.start_at),
                   "end_at": ts(data.end_at) if data.end_at else None, "reason": data.reason,
                   "comment": data.comment}
            changes = _diff(old, new)
            if not changes:
                return
            con.execute("""UPDATE downtimes SET equipment_id=?, work_date=?, shift=?, start_at=?, end_at=?,
                               reason=?, comment=?, updated_at=? WHERE id=?""", (*new.values(), now_ts(), downtime_id))
            _log_change(con, "update", "downtime", downtime_id, changes)

    def close_downtime(self, downtime_id: int, end_at: datetime) -> None:
        with self.transaction() as con:
            row = con.execute("SELECT * FROM downtimes WHERE id = ?", (downtime_id,)).fetchone()
            if row is None:
                raise NotFoundError("Простой не найден.")
            if row["end_at"] is not None:
                raise ConflictError("Простой уже завершён.")
            if end_at <= parse_ts(row["start_at"]):
                raise ConflictError("Время окончания должно быть позже начала простоя.")
            conflicts = self.find_overlapping_downtimes(con, row["equipment_id"], parse_ts(row["start_at"]),
                                                        end_at, exclude_id=downtime_id)
            if conflicts:
                raise ConflictError(self._overlap_message(conflicts))
            con.execute("UPDATE downtimes SET end_at = ?, updated_at = ? WHERE id = ?",
                        (ts(end_at), now_ts(), downtime_id))
            _log_change(con, "close", "downtime", downtime_id, {"end_at": ts(end_at)})

    def delete_downtime(self, downtime_id: int) -> None:
        with self.transaction() as con:
            old = _dict(con.execute("SELECT * FROM downtimes WHERE id = ?", (downtime_id,)).fetchone())
            if old is None:
                raise NotFoundError("Простой уже удалён.")
            con.execute("DELETE FROM downtimes WHERE id = ?", (downtime_id,))
            _log_change(con, "delete", "downtime", downtime_id, old)

    def shift_downtime_minutes(self, equipment_id: int, work_date: date, shift: str) -> tuple[float, int]:
        """Сумма завершённых простоев смены без двойного счёта и число открытых простоев."""
        from .intervals import Interval, union_minutes
        with self.connect(readonly=True) as con:
            rows = con.execute("""SELECT id, start_at, end_at FROM downtimes
                                  WHERE equipment_id = ? AND work_date = ? AND shift = ?""",
                               (equipment_id, work_date.isoformat(), shift)).fetchall()
        closed = [Interval(r["id"], equipment_id, parse_ts(r["start_at"]), parse_ts(r["end_at"]))
                  for r in rows if r["end_at"]]
        return union_minutes(closed), sum(1 for r in rows if not r["end_at"])

    # --- События источников данных -----------------------------------------------------

    @staticmethod
    def get_open_event(con: sqlite3.Connection, equipment_id: int) -> dict | None:
        return _dict(con.execute("""SELECT * FROM sensor_events WHERE equipment_id = ? AND end_at IS NULL
                                    ORDER BY start_at DESC, id DESC LIMIT 1""", (equipment_id,)).fetchone())

    @staticmethod
    def insert_event(con: sqlite3.Connection, equipment_id: int, state: str, start: datetime, *,
                     source_name: str, comment: str, downtime_id: int | None, received_at: datetime,
                     source: str = SOURCE_SIMULATOR) -> int:
        cur = con.execute("""INSERT INTO sensor_events (equipment_id, state, start_at, source, source_name,
                                 comment, downtime_id, received_at) VALUES (?,?,?,?,?,?,?,?)""",
                          (equipment_id, state, ts(start), source, source_name, comment, downtime_id,
                           ts(received_at)))
        return cur.lastrowid

    @staticmethod
    def close_event(con: sqlite3.Connection, event_id: int, end: datetime) -> None:
        con.execute("UPDATE sensor_events SET end_at = ? WHERE id = ? AND end_at IS NULL", (ts(end), event_id))

    @staticmethod
    def end_open_downtime(con: sqlite3.Connection, downtime_id: int, end: datetime, note: str = "") -> bool:
        """Завершает открытый простой; возвращает False, если он уже завершён или удалён."""
        cur = con.execute("""UPDATE downtimes SET end_at = ?, updated_at = ?,
                                 comment = CASE WHEN ? = '' THEN comment ELSE trim(comment || ' ' || ?) END
                             WHERE id = ? AND end_at IS NULL AND start_at <= ?""",
                          (ts(end), now_ts(), note, note, downtime_id, ts(end)))
        return cur.rowcount == 1

    @staticmethod
    def is_downtime_open(con: sqlite3.Connection, downtime_id: int | None) -> bool:
        if not downtime_id:
            return False
        row = con.execute("SELECT end_at FROM downtimes WHERE id = ?", (downtime_id,)).fetchone()
        return row is not None and row["end_at"] is None

    def current_state(self, equipment_id: int) -> dict | None:
        with self.connect(readonly=True) as con:
            return self.get_open_event(con, equipment_id)

    def list_events(self, limit: int = 300, equipment_id: int | None = None) -> list[dict]:
        query = """SELECT s.*, e.name AS equipment_name, d.reason AS downtime_reason
                   FROM sensor_events s JOIN equipment e ON e.id = s.equipment_id
                   LEFT JOIN downtimes d ON d.id = s.downtime_id"""
        params: list = []
        if equipment_id is not None:
            query += " WHERE s.equipment_id = ?"
            params.append(equipment_id)
        query += " ORDER BY s.start_at DESC, s.id DESC LIMIT ?"
        params.append(limit)
        with self.connect(readonly=True) as con:
            return [dict(r) for r in con.execute(query, params)]

    # --- Сгенерированные данные --------------------------------------------------------

    def delete_generated(self, source: str) -> dict[str, int]:
        """Удаляет ТОЛЬКО записи с source='demo' или 'simulator'. Ручной ввод не затрагивается."""
        if source not in (SOURCE_DEMO, SOURCE_SIMULATOR):
            raise ValueError("Удалять можно только демонстрационные или симулированные данные.")
        with self.transaction() as con:
            counts = {
                "sensor_events": con.execute("DELETE FROM sensor_events WHERE source = ?", (source,)).rowcount,
                "downtimes": con.execute("DELETE FROM downtimes WHERE source = ?", (source,)).rowcount,
                "production": con.execute("DELETE FROM production WHERE source = ?", (source,)).rowcount,
            }
            counts["equipment"] = con.execute("""DELETE FROM equipment WHERE is_demo = 1
                AND id NOT IN (SELECT equipment_id FROM production)
                AND id NOT IN (SELECT equipment_id FROM downtimes)
                AND id NOT IN (SELECT equipment_id FROM sensor_events)""").rowcount
            _log_change(con, "delete", source, None, counts)
            return counts

    def delete_rows(self, *, event_ids: list[int], downtime_ids: list[int], production_ids: list[int]) -> None:
        """Откат незавершённого сеанса симулятора (только записи с source='simulator')."""
        with self.transaction() as con:
            for table, ids in (("sensor_events", event_ids), ("downtimes", downtime_ids),
                               ("production", production_ids)):
                for chunk_start in range(0, len(ids), 500):
                    chunk = ids[chunk_start:chunk_start + 500]
                    marks = ",".join("?" * len(chunk))
                    con.execute(f"DELETE FROM {table} WHERE source = 'simulator' AND id IN ({marks})", chunk)
