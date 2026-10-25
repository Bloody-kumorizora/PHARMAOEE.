"""Пути к данным, пользовательские настройки, журналирование и общие константы."""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)

#: Переменная окружения, которой можно переопределить каталог данных.
ENV_DATA_DIR = "PHARMAOEE_DATA_DIR"
DB_FILENAME = "pharmaoee.db"
#: Каталог с исходным кодом проекта (здесь v8 хранила базу рядом с программой).
PROJECT_DIR = Path(__file__).resolve().parent.parent
LEGACY_DB_PATH = PROJECT_DIR / DB_FILENAME

# --- Смены -----------------------------------------------------------------
SHIFTS = ("1 смена", "2 смена", "3 смена")
#: Расписание смен по умолчанию: начало и длительность, мин.
#: Используется для подсказок в формах, автоопределения смены по времени
#: события и генерации демо-данных. 3 смена переходит через полночь и
#: относится к дате своего начала («дата смены»).
SHIFT_SCHEDULE = {
    "1 смена": (time(7, 0), 480),
    "2 смена": (time(15, 0), 480),
    "3 смена": (time(23, 0), 480),
}

# --- Происхождение данных ------------------------------------------------------
SOURCE_MANUAL = "manual"
SOURCE_DEMO = "demo"
SOURCE_SIMULATOR = "simulator"
SOURCES = (SOURCE_MANUAL, SOURCE_DEMO, SOURCE_SIMULATOR)
SOURCE_LABELS = {
    SOURCE_MANUAL: "Ручной ввод",
    SOURCE_DEMO: "Демо-данные",
    SOURCE_SIMULATOR: "Симулятор",
}
#: Фильтр по происхождению: все записи / только ручной ввод / только сгенерированные.
SOURCE_FILTERS = {
    "all": "Все данные",
    "manual": "Только ручной ввод",
    "generated": "Только демо и симулятор",
}

# --- Причины простоев по умолчанию -------------------------------------------
UNCLASSIFIED_REASON = "Не классифицировано"
DEFAULT_REASONS = (
    "Ожидание материала",
    "Переналадка",
    "Механическая неисправность",
    "Электрическая неисправность",
    "Контроль качества",
    "Отсутствие оператора",
    "Очистка / санитарная обработка",
    "Прочее",
)


def default_data_dir() -> Path:
    """Стандартный каталог пользовательских данных для текущей ОС."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / APP_NAME if base else Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "pharmaoee"


@dataclass(frozen=True)
class AppPaths:
    """Все пути, которые использует приложение. Данные отделены от программы."""

    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "pharmaoee.log"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def ensure(self) -> None:
        for path in (self.data_dir, self.backups_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def resolve_paths(cli_data_dir: str | None = None) -> AppPaths:
    """Порядок приоритета: аргумент --data-dir, переменная окружения, стандартный путь.

    Путь не зависит от текущего рабочего каталога, поэтому запуск из другой
    папки не создаёт новую пустую базу.
    """
    raw = cli_data_dir or os.environ.get(ENV_DATA_DIR)
    data_dir = Path(raw).expanduser() if raw else default_data_dir()
    return AppPaths(data_dir.resolve())


@dataclass
class Settings:
    """Небольшой набор пользовательских настроек, хранится в settings.json."""

    oee_threshold_pct: float = 60.0
    default_source_filter: str = "all"
    recent: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            log.warning("Файл настроек не прочитан (%s), используются значения по умолчанию", exc)
            return cls()
        settings = cls()
        try:
            threshold = float(raw.get("oee_threshold_pct", settings.oee_threshold_pct))
            if 0 < threshold < 100:
                settings.oee_threshold_pct = threshold
        except (TypeError, ValueError):
            pass
        if raw.get("default_source_filter") in SOURCE_FILTERS:
            settings.default_source_filter = raw["default_source_filter"]
        if isinstance(raw.get("recent"), dict):
            settings.recent = raw["recent"]
        return settings

    def save(self, path: Path) -> None:
        """Атомарная запись: сначала временный файл, затем замена."""
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def setup_logging(paths: AppPaths, level: int = logging.INFO) -> None:
    """Журнал операций и ошибок. Содержимое записей базы в журнал не пишется."""
    root = logging.getLogger()
    shutdown_logging()
    handler = RotatingFileHandler(paths.log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._pharmaoee = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def shutdown_logging() -> None:
    """Закрывает файловый журнал приложения (файл освобождается для удаления/переноса)."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_pharmaoee", False)]:
        root.removeHandler(handler)
        handler.close()
