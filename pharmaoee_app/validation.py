"""Разбор и проверка пользовательского ввода.

Правила валидации отделены от формул OEE: формулы только считают, а здесь
решается, какие значения допустимы. Ошибки (errors) блокируют сохранение,
предупреждения (warnings) требуют явного подтверждения пользователя.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping

from . import oee
from .config import SHIFT_SCHEDULE, SHIFTS

MAX_SHIFT_MIN = 24 * 60          # одна запись смены не может быть длиннее суток
MAX_COUNT = 100_000_000          # защита от опечаток с лишними разрядами
MAX_CYCLE_SEC = 24 * 3600
MAX_DOWNTIME = timedelta(days=7)
LONG_DOWNTIME = timedelta(hours=12)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_TEXT = 500

_DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_RU = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class FieldError(ValueError):
    """Ошибка разбора одного поля."""


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_date(text: str, label: str = "Дата") -> date:
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ."""
    value = (text or "").strip()
    if not value:
        raise FieldError(f"{label}: поле не заполнено.")
    try:
        if _DATE_ISO.match(value):
            return date.fromisoformat(value)
        match = _DATE_RU.match(value)
        if match:
            day, month, year = (int(g) for g in match.groups())
            return date(year, month, day)
    except ValueError:
        raise FieldError(f"{label}: такой даты не существует ({value}).") from None
    raise FieldError(f"{label}: введите дату в формате ДД.ММ.ГГГГ.")


def parse_time(text: str, label: str = "Время") -> time:
    value = (text or "").strip()
    match = _TIME.match(value)
    if not match:
        raise FieldError(f"{label}: введите время в формате ЧЧ:ММ.")
    hours, minutes, seconds = int(match[1]), int(match[2]), int(match[3] or 0)
    if hours > 23 or minutes > 59 or seconds > 59:
        raise FieldError(f"{label}: время {value} не существует.")
    return time(hours, minutes, seconds)


def parse_number(text: str, label: str, *, integer: bool = False) -> float | int:
    """Число с запятой или точкой; пробелы-разделители тысяч допускаются."""
    value = (text or "").strip().replace(" ", "").replace("\xa0", "").replace(" ", "")
    if not value:
        raise FieldError(f"{label}: поле не заполнено.")
    value = value.replace(",", ".")
    try:
        if integer:
            if not re.fullmatch(r"-?\d+", value):
                raise ValueError
            return int(value)
        number = float(value)
    except ValueError:
        kind = "целое число" if integer else "число"
        raise FieldError(f"{label}: введите {kind}.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise FieldError(f"{label}: недопустимое значение.")
    return number


def parse_date_range(start_text: str, end_text: str, max_days: int | None = None) -> tuple[date, date]:
    start = parse_date(start_text, "Начальная дата")
    end = parse_date(end_text, "Конечная дата")
    if end < start:
        raise FieldError("Конечная дата не может быть раньше начальной.")
    if max_days is not None and (end - start).days + 1 > max_days:
        raise FieldError(f"Период слишком большой: не более {max_days} дней за одну операцию.")
    return start, end


def dates_inclusive(start: date, end: date):
    """Даты от start до end включительно; обратный период запрещён."""
    if end < start:
        raise ValueError("Конечная дата не может быть раньше начальной.")
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def normalize_name(text: str) -> str:
    return " ".join((text or "").split())


def shift_bounds(work_date: date, shift: str) -> tuple[datetime, datetime]:
    start_time, length = SHIFT_SCHEDULE[shift]
    start = datetime.combine(work_date, start_time)
    return start, start + timedelta(minutes=length)


def shift_for(moment: datetime) -> tuple[date, str]:
    """Дата смены и смена по расписанию для момента времени (ночная смена — по дате начала)."""
    for shift in SHIFTS:
        for day in (moment.date(), moment.date() - timedelta(days=1)):
            start, end = shift_bounds(day, shift)
            if start <= moment < end:
                return day, shift
    return moment.date(), SHIFTS[0]


# --- Производственные записи ---------------------------------------------------

@dataclass(frozen=True)
class ProductionInput:
    equipment_id: int
    work_date: date
    shift: str
    planned_min: float
    run_min: float
    total_count: int
    good_count: int
    ideal_cycle_sec: float
    note: str = ""


def validate_production(raw: Mapping[str, str], equipment_id: int | None,
                        today: date) -> tuple[ProductionInput | None, Report]:
    """raw: work_date, shift, planned, run, total, good, cycle, note (строки из формы)."""
    report = Report()
    values: dict = {}
    if equipment_id is None:
        report.errors.append("Выберите оборудование.")
    parsers = [
        ("work_date", lambda: parse_date(raw.get("work_date", ""), "Дата смены")),
        ("planned", lambda: parse_number(raw.get("planned", ""), "Плановое время")),
        ("run", lambda: parse_number(raw.get("run", ""), "Время работы")),
        ("total", lambda: parse_number(raw.get("total", ""), "Произведено", integer=True)),
        ("good", lambda: parse_number(raw.get("good", ""), "Годные", integer=True)),
        ("cycle", lambda: parse_number(raw.get("cycle", ""), "Идеальный цикл")),
    ]
    for key, parser in parsers:
        try:
            values[key] = parser()
        except FieldError as exc:
            report.errors.append(str(exc))
    shift = (raw.get("shift") or "").strip()
    if shift not in SHIFTS:
        report.errors.append("Выберите смену из списка.")
    note = (raw.get("note") or "").strip()
    if len(note) > MAX_TEXT:
        report.errors.append(f"Комментарий длиннее {MAX_TEXT} символов.")
    if report.errors:
        return None, report

    planned, run = values["planned"], values["run"]
    total, good, cycle = values["total"], values["good"], values["cycle"]
    if planned <= 0:
        report.errors.append("Плановое время должно быть больше нуля.")
    elif planned > MAX_SHIFT_MIN:
        report.errors.append("Плановое время одной записи не может превышать 1440 мин (сутки).")
    if run < 0:
        report.errors.append("Время работы не может быть отрицательным.")
    elif planned > 0 and run > planned:
        report.errors.append("Время работы больше планового времени: доступность была бы выше 100%. "
                             "Проверьте плановое время и учёт простоев.")
    if total < 0 or good < 0:
        report.errors.append("Количество единиц не может быть отрицательным.")
    elif good > total:
        report.errors.append("Годных единиц не может быть больше, чем произведено.")
    if total > MAX_COUNT:
        report.errors.append("Слишком большое количество единиц — проверьте ввод.")
    if cycle <= 0:
        report.errors.append("Идеальное время цикла должно быть больше нуля.")
    elif cycle > MAX_CYCLE_SEC:
        report.errors.append("Идеальное время цикла не может превышать сутки.")
    if run == 0 and total > 0:
        report.errors.append("Есть выпуск, но время работы равно нулю — данные противоречивы.")
    if report.errors:
        return None, report

    if values["work_date"] > today:
        report.warnings.append("Дата смены находится в будущем.")
    result = oee.calculate(planned, run, total, good, cycle)
    if oee.FLAG_PERFORMANCE_OVER in result.flags:
        report.warnings.append(f"Производительность {result.performance * 100:.1f}% — выше 100%. "
                               "Проверьте идеальное время цикла, состав продукции и исходные данные.")
    if run == 0:
        report.warnings.append("Время работы равно нулю: OEE будет 0%.")
    data = ProductionInput(equipment_id, values["work_date"], shift, float(planned), float(run),
                           int(total), int(good), float(cycle), note)
    return data, report


# --- Простои -------------------------------------------------------------------

@dataclass(frozen=True)
class DowntimeInput:
    equipment_id: int
    work_date: date
    shift: str
    start_at: datetime
    end_at: datetime | None     # None — простой продолжается
    reason: str
    comment: str = ""

    @property
    def minutes(self) -> float | None:
        if self.end_at is None:
            return None
        return (self.end_at - self.start_at).total_seconds() / 60.0


def validate_downtime(raw: Mapping, equipment_id: int | None, now: datetime,
                      reasons: set[str] | None = None) -> tuple[DowntimeInput | None, Report]:
    """raw: work_date, shift, start_date, start_time, end_date, end_time, ongoing, reason, comment."""
    report = Report()
    if equipment_id is None:
        report.errors.append("Выберите оборудование.")
    try:
        work_date = parse_date(raw.get("work_date", ""), "Дата смены")
    except FieldError as exc:
        report.errors.append(str(exc))
        work_date = None
    shift = (raw.get("shift") or "").strip()
    if shift not in SHIFTS:
        report.errors.append("Выберите смену из списка.")
    start_at = end_at = None
    try:
        start_at = datetime.combine(parse_date(raw.get("start_date", ""), "Дата начала"),
                                    parse_time(raw.get("start_time", ""), "Время начала"))
    except FieldError as exc:
        report.errors.append(str(exc))
    ongoing = bool(raw.get("ongoing"))
    if not ongoing:
        try:
            end_at = datetime.combine(parse_date(raw.get("end_date", ""), "Дата окончания"),
                                      parse_time(raw.get("end_time", ""), "Время окончания"))
        except FieldError as exc:
            report.errors.append(str(exc) + " Если простой ещё идёт, отметьте «Простой продолжается».")
    reason = (raw.get("reason") or "").strip()
    if not reason:
        report.errors.append("Укажите причину простоя (или «Не классифицировано»).")
    elif reasons is not None and reason not in reasons:
        report.errors.append("Причина отсутствует в справочнике.")
    comment = (raw.get("comment") or "").strip()
    if len(comment) > MAX_TEXT:
        report.errors.append(f"Комментарий длиннее {MAX_TEXT} символов.")
    if report.errors:
        return None, report

    if start_at > now + FUTURE_TOLERANCE:
        report.errors.append("Начало простоя не может быть в будущем.")
    if end_at is not None:
        if end_at <= start_at:
            if end_at.date() == start_at.date() and end_at.time() < start_at.time():
                report.errors.append("Окончание раньше начала. Если простой перешёл через полночь, "
                                     "укажите дату окончания следующим днём.")
            else:
                report.errors.append("Окончание должно быть позже начала.")
        elif end_at - start_at > MAX_DOWNTIME:
            report.errors.append("Простой длиннее 7 суток — разбейте его на несколько записей по сменам.")
        if end_at > now + FUTURE_TOLERANCE:
            report.errors.append("Окончание простоя не может быть в будущем. "
                                 "Если простой ещё идёт, отметьте «Простой продолжается».")
    if report.errors:
        return None, report

    if end_at is not None and end_at - start_at > LONG_DOWNTIME:
        report.warnings.append("Простой длиннее 12 часов — проверьте даты начала и окончания.")
    if abs((start_at.date() - work_date).days) > 1:
        report.warnings.append("Дата смены отличается от даты начала простоя больше чем на сутки.")
    return DowntimeInput(equipment_id, work_date, shift, start_at, end_at, reason, comment), report


# --- Оборудование ---------------------------------------------------------------

@dataclass(frozen=True)
class EquipmentInput:
    name: str
    area: str = ""
    ideal_cycle_sec: float | None = None
    note: str = ""
    is_active: bool = True


def validate_equipment(raw: Mapping) -> tuple[EquipmentInput | None, Report]:
    report = Report()
    name = normalize_name(raw.get("name", ""))
    if not name:
        report.errors.append("Введите название оборудования.")
    elif len(name) > 120:
        report.errors.append("Название длиннее 120 символов.")
    cycle_text = (raw.get("cycle") or "").strip()
    cycle = None
    if cycle_text:
        try:
            cycle = float(parse_number(cycle_text, "Идеальный цикл"))
            if cycle <= 0 or cycle > MAX_CYCLE_SEC:
                report.errors.append("Идеальный цикл должен быть больше нуля и не больше суток.")
        except FieldError as exc:
            report.errors.append(str(exc))
    area = normalize_name(raw.get("area", ""))
    note = (raw.get("note") or "").strip()
    if len(note) > MAX_TEXT or len(area) > 120:
        report.errors.append("Слишком длинный текст в поле участка или примечания.")
    if report.errors:
        return None, report
    return EquipmentInput(name, area, cycle, note, bool(raw.get("is_active", True))), report
