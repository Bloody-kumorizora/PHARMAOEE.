"""Единое форматирование чисел, дат и длительностей для интерфейса и отчётов."""
from __future__ import annotations

from datetime import date, datetime

NBSP = " "  # узкий неразрывный пробел — разделитель тысяч


def pct(value: float | None, digits: int = 1) -> str:
    """0.853 → '85,3%'; None → '—'."""
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%".replace(".", ",")


def integer(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", NBSP)


def number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", NBSP).replace(".", ",")
    return text


def minutes(value: float | None) -> str:
    """Минуты → '1 ч 05 мин' / '45 мин'."""
    if value is None:
        return "—"
    total = int(round(value))
    sign = "−" if total < 0 else ""
    total = abs(total)
    hours, mins = divmod(total, 60)
    if hours:
        return f"{sign}{hours} ч {mins:02d} мин"
    return f"{sign}{mins} мин"


def ru_date(value: date | str | None) -> str:
    """ISO-дата или date → 'ДД.ММ.ГГГГ'."""
    if not value:
        return ""
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value.strftime("%d.%m.%Y")


def ru_datetime(value: datetime | str | None, with_seconds: bool = False) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M")


def short_date(value: date) -> str:
    return value.strftime("%d.%m")
