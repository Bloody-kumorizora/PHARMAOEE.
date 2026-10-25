"""Резервное копирование и восстановление базы SQLite.

Копия создаётся через backup API SQLite (согласованный снимок даже при
активной базе), а не простым копированием файла. Восстановление:
проверка файла → копия текущей базы → подготовка во временном файле →
атомарная замена содержимого → откат при ошибке.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .db import LEGACY_TABLES, SCHEMA_TABLES, SCHEMA_VERSION, Database, DatabaseError, describe_error

log = logging.getLogger(__name__)

SQLITE_HEADER = b"SQLite format 3\x00"


class BackupError(DatabaseError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size_bytes: int
    schema_version: int
    kind: str                    # "v9" или "v8"
    counts: dict[str, int]
    modified: datetime

    @property
    def kind_label(self) -> str:
        return "PharmaOEE v9" if self.kind == "v9" else "PharmaOEE v8 (будет обновлена)"


@dataclass(frozen=True)
class RestoreResult:
    restored_from: Path
    safety_backup: Path
    info: BackupInfo


def timestamped_name(label: str) -> str:
    return f"pharmaoee_{label}_{datetime.now():%Y-%m-%d_%H-%M-%S}.db"


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)


def create_backup(db_path: Path, destination: Path) -> Path:
    """Согласованная копия базы. Пишется во временный файл, проверяется и только потом
    переименовывается — частично записанная копия не появится под итоговым именем."""
    db_path, destination = Path(db_path), Path(destination)
    if not db_path.exists():
        raise BackupError("Файл базы данных не найден — копировать нечего.")
    if destination.resolve() == db_path.resolve():
        raise BackupError("Нельзя сохранить резервную копию поверх рабочей базы.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        if partial.exists():
            partial.unlink()
        source = _connect_ro(db_path)
        try:
            target = sqlite3.connect(partial)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        inspect_backup(partial)
        os.replace(partial, destination)
    except BackupError:
        _remove_quietly(partial)
        raise
    except (OSError, sqlite3.Error) as exc:
        _remove_quietly(partial)
        raise BackupError("Резервная копия не создана. " + describe_error(exc)) from exc
    log.info("Создана резервная копия: %s", destination)
    return destination


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Не удалось удалить временный файл %s: %s", path, exc)


def inspect_backup(path: Path) -> BackupInfo:
    """Проверяет, что файл — целая база PharmaOEE поддерживаемой версии."""
    path = Path(path)
    if not path.is_file():
        raise BackupError("Файл не найден.")
    size = path.stat().st_size
    with open(path, "rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise BackupError("Файл не является базой данных SQLite.")
    try:
        con = _connect_ro(path)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise BackupError(f"Файл повреждён (проверка целостности: {check}).")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version > SCHEMA_VERSION:
                raise BackupError(f"Копия создана более новой версией PharmaOEE (схема {version}).")
            if version == SCHEMA_VERSION and all(t in tables for t in SCHEMA_TABLES):
                kind, counted = "v9", ("equipment", "production", "downtimes", "sensor_events")
            elif version == 0 and "production" in tables and "downtimes" in tables and "equipment" not in tables:
                kind, counted = "v8", tuple(t for t in LEGACY_TABLES if t in tables)
            else:
                raise BackupError("Файл не похож на базу PharmaOEE: неожиданный набор таблиц.")
            counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counted}
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BackupError("Файл не удалось прочитать как базу PharmaOEE. " + describe_error(exc)) from exc
    return BackupInfo(path, size, version, kind, counts, datetime.fromtimestamp(path.stat().st_mtime))


def list_backups(backups_dir: Path) -> list[Path]:
    backups_dir = Path(backups_dir)
    if not backups_dir.is_dir():
        return []
    return sorted((p for p in backups_dir.glob("*.db") if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def restore_backup(db: Database, source: Path, backups_dir: Path) -> RestoreResult:
    """Восстанавливает базу из файла. При любой ошибке текущие данные сохраняются."""
    source = Path(source)
    info = inspect_backup(source)
    if source.resolve() == db.path.resolve():
        raise BackupError("Выбран файл рабочей базы — восстанавливать нечего.")
    safety = create_backup(db.path, Path(backups_dir) / timestamped_name("before_restore"))
    staging = db.path.with_name(db.path.name + ".restore-tmp")
    _remove_quietly(staging)
    try:
        # 1. Копия выбранного файла во временную базу и обновление схемы при необходимости.
        src = _connect_ro(source)
        try:
            tmp = sqlite3.connect(staging)
            try:
                src.backup(tmp)
            finally:
                tmp.close()
        finally:
            src.close()
        Database(staging).initialize(backups_dir=None)
        if Database(staging).quick_check() != "ok":
            raise BackupError("Подготовленная копия не прошла проверку целостности.")
        # 2. Перенос содержимого в рабочую базу одним шагом backup API (атомарно для SQLite).
        _copy_into(staging, db.path)
    except BaseException as exc:
        log.error("Восстановление не выполнено: %s", exc)
        try:
            if db.path.exists() and Database(db.path).quick_check() != "ok":
                _copy_into(safety, db.path)
        except (OSError, sqlite3.Error) as rollback_exc:
            log.critical("Не удалось вернуть копию %s: %s", safety, rollback_exc)
        if isinstance(exc, (BackupError, DatabaseError)):
            raise
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise BackupError("Восстановление не выполнено, текущая база сохранена. "
                              + describe_error(exc)) from exc
        raise
    finally:
        _remove_quietly(staging)
    log.info("База восстановлена из %s; копия прежней базы: %s", source, safety)
    return RestoreResult(source, safety, info)


def _copy_into(source: Path, target: Path) -> None:
    src = _connect_ro(source)
    try:
        dst = sqlite3.connect(target, timeout=5.0)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
