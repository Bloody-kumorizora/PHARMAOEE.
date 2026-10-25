"""Генератор демонстрационных данных.

* Данные строятся детерминированно: одинаковые настройки (зерно, оборудование,
  дата, смена) всегда дают одинаковые записи — независимо от длины периода.
* Существующие смены (дата + смена + оборудование) пропускаются, поэтому
  повторный запуск не создаёт дубликатов.
* Все записи помечаются source='demo' и пишутся только на демо-оборудование
  (is_demo = 1); удалить их можно, не затрагивая ручной ввод.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import SHIFT_SCHEDULE, SHIFTS, SOURCE_DEMO
from .db import Database, _log_change
from .validation import DowntimeInput, ProductionInput, dates_inclusive, shift_bounds

MAX_DAYS = 366
DEFAULT_SEED = 2026


@dataclass(frozen=True)
class DemoEquipment:
    name: str
    area: str
    ideal_cycle_sec: float
    speed: tuple[float, float]       # диапазон фактической скорости относительно идеальной
    defect_rate: tuple[float, float]
    stop_weights: dict[str, int]


DEMO_EQUIPMENT = (
    DemoEquipment("ДЕМО — Блистерная линия 1", "Упаковка", 1.2, (0.86, 0.95), (0.004, 0.015),
                  {"Переналадка": 4, "Ожидание материала": 3, "Механическая неисправность": 2,
                   "Контроль качества": 1, "Очистка / санитарная обработка": 2}),
    DemoEquipment("ДЕМО — Таблеточный пресс 2", "Таблетирование", 0.45, (0.82, 0.93), (0.006, 0.02),
                  {"Механическая неисправность": 3, "Очистка / санитарная обработка": 3, "Переналадка": 2,
                   "Электрическая неисправность": 1, "Ожидание материала": 2}),
    DemoEquipment("ДЕМО — Линия фасовки 3", "Фасовка", 2.0, (0.88, 0.97), (0.002, 0.01),
                  {"Ожидание материала": 4, "Отсутствие оператора": 1, "Переналадка": 2,
                   "Электрическая неисправность": 1, "Прочее": 1}),
)
DEMO_BY_NAME = {e.name: e for e in DEMO_EQUIPMENT}


@dataclass(frozen=True)
class DemoRequest:
    start: date
    end: date
    equipment: tuple[str, ...] = tuple(e.name for e in DEMO_EQUIPMENT)
    shifts: tuple[str, ...] = SHIFTS[:1]
    seed: int = DEFAULT_SEED

    def validate(self, today: date) -> None:
        if self.end < self.start:
            raise ValueError("Конечная дата не может быть раньше начальной.")
        days = (self.end - self.start).days + 1
        if days > MAX_DAYS:
            raise ValueError(f"Период {days} дн. — больше допустимых {MAX_DAYS} дней за одну операцию.")
        if self.end > today:
            raise ValueError("Демо-данные создаются только для прошедших дат и сегодняшнего дня.")
        if not self.equipment:
            raise ValueError("Выберите хотя бы одну демо-линию.")
        if not self.shifts:
            raise ValueError("Выберите хотя бы одну смену.")
        unknown = [n for n in self.equipment if n not in DEMO_BY_NAME]
        if unknown:
            raise ValueError("Неизвестное демо-оборудование: " + ", ".join(unknown))

    @property
    def planned_shifts(self) -> int:
        return ((self.end - self.start).days + 1) * len(self.equipment) * len(self.shifts)


@dataclass(frozen=True)
class DemoShift:
    equipment: str
    work_date: date
    shift: str
    planned_min: float
    run_min: float
    total_count: int
    good_count: int
    ideal_cycle_sec: float
    scenario: str
    downtimes: tuple[tuple[datetime, datetime, str], ...]


@dataclass(frozen=True)
class DemoResult:
    created_shifts: int
    created_downtimes: int
    skipped: int
    skipped_unfinished: int
    start: date
    end: date


SCENARIOS = (("Обычная смена", 70), ("Много переналадок", 12), ("Поломка", 8),
             ("Проблема качества", 6), ("Стабильная смена", 4))


def plan_shift(spec: DemoEquipment, work_date: date, shift: str, seed: int) -> DemoShift:
    """Одна детерминированная демо-смена с непересекающимися простоями внутри смены."""
    rng = random.Random(f"{seed}|{spec.name}|{work_date.isoformat()}|{shift}")
    scenario = rng.choices([s for s, _ in SCENARIOS], weights=[w for _, w in SCENARIOS])[0]
    planned = SHIFT_SCHEDULE[shift][1]
    reasons, weights = list(spec.stop_weights), list(spec.stop_weights.values())

    stops: list[tuple[float, str]] = []
    count = {"Стабильная смена": rng.randint(0, 1), "Много переналадок": rng.randint(3, 5)}.get(
        scenario, rng.randint(1, 3))
    for _ in range(count):
        reason = "Переналадка" if scenario == "Много переналадок" and rng.random() < 0.7 else \
            rng.choices(reasons, weights=weights)[0]
        stops.append((rng.randint(5, 35), reason))
    if scenario == "Поломка":
        stops.append((rng.randint(60, 150), rng.choice(["Механическая неисправность",
                                                        "Электрическая неисправность"])))
    rng.shuffle(stops)

    # Раскладываем простои по смене: случайные промежутки работы между ними.
    shift_start, _ = shift_bounds(work_date, shift)
    total_stop = sum(d for d, _ in stops)
    free = max(0.0, planned - total_stop - 10)
    cuts = sorted(rng.uniform(0, free) for _ in stops)
    downtimes = []
    cursor, previous = 0.0, 0.0
    for (duration, reason), cut in zip(stops, cuts):
        cursor += cut - previous
        previous = cut
        start = shift_start + timedelta(minutes=int(cursor))
        end = start + timedelta(minutes=duration)
        downtimes.append((start, end, reason))
        cursor += duration
    run = planned - total_stop

    speed = rng.uniform(*spec.speed)
    defect = rng.uniform(*spec.defect_rate) * (rng.uniform(3, 6) if scenario == "Проблема качества" else 1)
    total = int(run * 60 / spec.ideal_cycle_sec * speed)
    good = total - int(round(total * defect))
    return DemoShift(spec.name, work_date, shift, float(planned), float(run), total, good,
                     spec.ideal_cycle_sec, scenario, tuple(downtimes))


def plan(request: DemoRequest) -> list[DemoShift]:
    return [plan_shift(DEMO_BY_NAME[name], day, shift, request.seed)
            for day in dates_inclusive(request.start, request.end)
            for name in request.equipment
            for shift in request.shifts]


def generate(db: Database, request: DemoRequest, now: datetime | None = None) -> DemoResult:
    """Записывает демо-данные одной транзакцией; существующие и ещё не завершённые смены пропускает."""
    now = now or datetime.now()
    request.validate(now.date())
    created = downtimes = skipped = unfinished = 0
    with db.transaction() as con:
        equipment_ids = {spec.name: db.ensure_equipment(con, spec.name, area=spec.area,
                                                        ideal_cycle_sec=spec.ideal_cycle_sec, is_demo=True)
                         for spec in DEMO_EQUIPMENT if spec.name in request.equipment}
        for item in plan(request):
            eq_id = equipment_ids[item.equipment]
            if shift_bounds(item.work_date, item.shift)[1] > now:
                unfinished += 1
                continue
            if db.find_duplicate_production(con, item.work_date, item.shift, eq_id):
                skipped += 1
                continue
            if any(db.find_overlapping_downtimes(con, eq_id, s, e) for s, e, _ in item.downtimes):
                skipped += 1
                continue
            db.insert_production(con, ProductionInput(
                eq_id, item.work_date, item.shift, item.planned_min, item.run_min, item.total_count,
                item.good_count, item.ideal_cycle_sec, f"Демо-сценарий: {item.scenario}"), SOURCE_DEMO)
            for start, end, reason in item.downtimes:
                db.insert_downtime(con, DowntimeInput(eq_id, item.work_date, item.shift, start, end, reason,
                                                      "Демо-запись, не факт производства"), SOURCE_DEMO)
                downtimes += 1
            created += 1
        _log_change(con, "create", "demo", None,
                    {"start": request.start, "end": request.end, "seed": request.seed,
                     "shifts": created, "downtimes": downtimes, "skipped": skipped})
    return DemoResult(created, downtimes, skipped, unfinished, request.start, request.end)


def ensure_demo_equipment(db: Database) -> list[int]:
    """Создаёт демо-линии, если их ещё нет (для симулятора)."""
    with db.transaction() as con:
        return [db.ensure_equipment(con, spec.name, area=spec.area, ideal_cycle_sec=spec.ideal_cycle_sec,
                                    is_demo=True) for spec in DEMO_EQUIPMENT]
