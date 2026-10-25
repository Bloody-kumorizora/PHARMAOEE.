"""Точка входа: пути к данным, журнал, проверка и миграция базы, запуск окна."""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import APP_NAME, __version__
from .backup import BackupError, create_backup, inspect_backup
from .config import LEGACY_DB_PATH, AppPaths, Settings, resolve_paths, setup_logging
from .db import Database, DatabaseError, MigrationError, describe_error

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pharmaoee", description=f"{APP_NAME} {__version__} — учебный "
                                                                   "прототип мониторинга OEE и простоев.")
    parser.add_argument("--data-dir", help="каталог данных (база, резервные копии, журнал, настройки)")
    parser.add_argument("--check", action="store_true",
                        help="без окна: подготовить и проверить базу, вывести сведения и выйти")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def import_legacy_db(paths: AppPaths) -> str | None:
    """Если новой базы ещё нет, а рядом с программой лежит база v8, копирует её (оригинал не меняется)."""
    if paths.db_path.exists() or not LEGACY_DB_PATH.exists() or LEGACY_DB_PATH.resolve() == paths.db_path.resolve():
        return None
    try:
        create_backup(LEGACY_DB_PATH, paths.db_path)
    except BackupError as exc:
        log.error("База v8 %s не импортирована: %s", LEGACY_DB_PATH, exc)
        return (f"Найдена база предыдущей версии ({LEGACY_DB_PATH}), но импортировать её не удалось: {exc} "
                "Создана новая база; исходный файл не изменён.")
    log.info("База v8 %s скопирована в %s", LEGACY_DB_PATH, paths.db_path)
    return (f"Найдена база предыдущей версии ({LEGACY_DB_PATH}). Она скопирована в каталог данных и обновлена; "
            "исходный файл не изменён.")


def prepare_database(paths: AppPaths) -> tuple[Database, list[str]]:
    notes: list[str] = []
    legacy = import_legacy_db(paths)
    if legacy:
        notes.append(legacy)
    db = Database(paths.db_path)
    result = db.initialize(paths.backups_dir)
    if result.created:
        log.info("Создана новая база: %s", db.path)
    if result.migrated_from:
        notes.append(f"База обновлена с версии {result.migrated_from}. Копия до обновления: {result.backup_path}")
    return db, notes


def run_check(paths: AppPaths) -> int:
    """Проверка без графического окружения (удобно для Linux-серверов и CI)."""
    try:
        db, notes = prepare_database(paths)
        check = db.quick_check()
        stats = db.stats()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"ОШИБКА: {describe_error(exc)}", file=sys.stderr)
        return 1
    print(f"{APP_NAME} {__version__}")
    print(f"Каталог данных: {paths.data_dir}")
    print(f"База: {db.path} (схема {stats['schema_version']}, проверка: {check})")
    print(f"Оборудование: {stats['equipment']}, смен: {stats['production']}, простоев: {stats['downtimes']}, "
          f"событий: {stats['sensor_events']}")
    for note in notes:
        print(note)
    try:
        import tkinter
        print(f"Tkinter: доступен (Tk {tkinter.TkVersion})")
    except ImportError:
        print("Tkinter: НЕ установлен — графический интерфейс не запустится")
    return 0 if check == "ok" else 1


def _fatal(title: str, message: str) -> int:
    log.error("%s: %s", title, message)
    print(f"{title}: {message}", file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except Exception:          # noqa: BLE001 — окна может не быть (нет дисплея или Tkinter)
        pass
    return 1


def recover_corrupted(root, paths: AppPaths, reason: str) -> Database | None:
    """Повреждённая база: отложить файл в сторону и восстановить из выбранной копии."""
    from tkinter import filedialog, messagebox
    if not messagebox.askyesno("База данных повреждена",
                               f"{reason}\n\nВыбрать резервную копию для восстановления?\n"
                               "Повреждённый файл не удаляется: он будет перемещён в папку резервных копий.",
                               icon="error", parent=root):
        return None
    source = filedialog.askopenfilename(parent=root, title="Выберите резервную копию",
                                        initialdir=str(paths.backups_dir),
                                        filetypes=[("База SQLite", "*.db"), ("Все файлы", "*.*")])
    if not source:
        return None
    try:
        inspect_backup(Path(source))
        aside = paths.backups_dir / f"pharmaoee_corrupted_{datetime.now():%Y-%m-%d_%H-%M-%S}.db"
        os.replace(paths.db_path, aside)
        create_backup(Path(source), paths.db_path)
        db = Database(paths.db_path)
        db.initialize(paths.backups_dir)
    except (BackupError, DatabaseError, OSError, sqlite3.Error) as exc:
        messagebox.showerror("Восстановление не выполнено", describe_error(exc), parent=root)
        return None
    messagebox.showinfo("Восстановление выполнено", f"База восстановлена из {source}.\nПовреждённый файл: {aside}",
                        parent=root)
    return db


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = resolve_paths(args.data_dir)
    try:
        paths.ensure()
    except OSError as exc:
        return _fatal("Нет доступа к каталогу данных", f"{paths.data_dir}\n{describe_error(exc)}\n\n"
                      "Укажите другой каталог параметром --data-dir или переменной PHARMAOEE_DATA_DIR.")
    setup_logging(paths)
    log.info("Запуск %s %s, данные: %s", APP_NAME, __version__, paths.data_dir)
    if args.check:
        return run_check(paths)

    try:
        import tkinter as tk
    except ImportError:
        return _fatal("Tkinter не найден", "Установите пакет Tkinter для вашей версии Python (см. README).")
    try:
        db, notes = prepare_database(paths)
        corrupted = None if db.quick_check() == "ok" else "Проверка целостности базы не пройдена."
    except MigrationError as exc:
        return _fatal("База данных не обновлена", describe_error(exc))
    except (DatabaseError, sqlite3.DatabaseError) as exc:
        db, notes, corrupted = None, [], describe_error(exc)
    except OSError as exc:
        return _fatal("Ошибка доступа к базе", describe_error(exc))

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        return _fatal("Нет графического окружения", f"Окно не может быть открыто: {exc}. "
                      "Для проверки без окна запустите с параметром --check.")
    root.withdraw()
    if corrupted:
        db = recover_corrupted(root, paths, corrupted)
        if db is None:
            root.destroy()
            return 1
    from .ui.app import App
    settings = Settings.load(paths.settings_path)
    App(root, paths, db, settings, notes)
    root.deiconify()
    root.mainloop()
    return 0
