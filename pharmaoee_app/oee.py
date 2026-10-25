"""Расчёт OEE (Overall Equipment Effectiveness — общая эффективность оборудования).

Формулы (для одной записи смены):

* Доступность (Availability)      A = время работы / плановое время
* Производительность (Performance) P = произведено × идеальный цикл / время работы
* Качество (Quality)               Q = годные / произведено
* OEE                              OEE = A × P × Q = годные × идеальный цикл / плановое время

Для нескольких записей суммируются минуты и «идеальное время» выпуска
(Σ штук × цикл), поэтому записи с разным циклом объединяются корректно.
Качество при агрегировании взвешивается идеальным временем:
Q = Σ(годные × цикл) / Σ(произведено × цикл). При одинаковом цикле это ровно
«годные / произведено»; при разном — гарантирует A × P × Q = OEE.
Доля годных в штуках доступна отдельно (``good_share``).

Модуль только считает. Значения не обрезаются до 100%: выход за пределы
сообщается флагами, а решение о допустимости ввода принимает модуль validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

EPS = 1e-9

# Флаги — признаки, требующие внимания пользователя.
FLAG_NO_PLANNED = "no_planned_time"
FLAG_NO_RUN = "no_run_time"
FLAG_NO_OUTPUT = "no_output"
FLAG_NEGATIVE = "negative_input"
FLAG_AVAILABILITY_OVER = "availability_over_100"
FLAG_PERFORMANCE_OVER = "performance_over_100"
FLAG_GOOD_OVER_TOTAL = "good_over_total"
FLAG_NO_CYCLE = "no_ideal_cycle"

FLAG_MESSAGES = {
    FLAG_NO_PLANNED: "Плановое время равно нулю — показатели не рассчитываются.",
    FLAG_NO_RUN: "Время работы равно нулю — производительность не определена.",
    FLAG_NO_OUTPUT: "Нет выпуска — качество не определено.",
    FLAG_NEGATIVE: "Есть отрицательные значения — расчёт невозможен, проверьте исходные данные.",
    FLAG_AVAILABILITY_OVER: "Доступность выше 100%: время работы больше планового. "
                            "Проверьте определение планового времени и учёт интервалов.",
    FLAG_PERFORMANCE_OVER: "Производительность выше 100%: проверьте идеальное время цикла, "
                           "состав продукции и исходные данные.",
    FLAG_GOOD_OVER_TOTAL: "Годных единиц больше, чем произведено, — данные противоречивы.",
    FLAG_NO_CYCLE: "Идеальное время цикла не задано — производительность и OEE не рассчитываются.",
}

#: Флаги, которые означают ошибку данных (а не просто пустой период).
ERROR_FLAGS = frozenset({FLAG_NEGATIVE, FLAG_AVAILABILITY_OVER, FLAG_PERFORMANCE_OVER,
                         FLAG_GOOD_OVER_TOTAL, FLAG_NO_CYCLE})


@dataclass(frozen=True)
class OEEResult:
    """Итог расчёта. Коэффициенты — доли (0.85 = 85%) или None, если не определены."""

    records: int
    planned_min: float
    run_min: float
    total_count: int
    good_count: int
    ideal_output_min: float      # Σ произведено × цикл, мин
    good_output_min: float       # Σ годные × цикл, мин (полностью продуктивное время)
    availability: float | None
    performance: float | None
    quality: float | None
    oee: float | None
    flags: frozenset[str]

    @property
    def defect_count(self) -> int:
        return self.total_count - self.good_count

    @property
    def good_share(self) -> float | None:
        """Доля годных по штукам."""
        return self.good_count / self.total_count if self.total_count > 0 else None

    @property
    def lost_time_min(self) -> float:
        """Потери доступности: плановое время − время работы."""
        return self.planned_min - self.run_min

    @property
    def has_data(self) -> bool:
        return self.records > 0

    @property
    def warnings(self) -> list[str]:
        return [FLAG_MESSAGES[f] for f in sorted(self.flags) if f in ERROR_FLAGS]


EMPTY = OEEResult(0, 0.0, 0.0, 0, 0, 0.0, 0.0, None, None, None, None, frozenset())


def _value(row: Mapping, key: str) -> float:
    raw = row[key]
    return 0.0 if raw is None else float(raw)


def _from_totals(records: int, planned: float, run: float, total: int, good: int,
                 ideal_min: float, good_ideal_min: float, flags: set[str]) -> OEEResult:
    if records == 0:
        return EMPTY
    if planned <= EPS:
        flags.add(FLAG_NO_PLANNED)
    if run <= EPS:
        flags.add(FLAG_NO_RUN)
    if total <= 0:
        flags.add(FLAG_NO_OUTPUT)
    if FLAG_NEGATIVE in flags:
        return OEEResult(records, planned, run, total, good, ideal_min, good_ideal_min,
                         None, None, None, None, frozenset(flags))

    availability = run / planned if planned > EPS else None
    performance = ideal_min / run if run > EPS and FLAG_NO_CYCLE not in flags else None
    quality = good_ideal_min / ideal_min if ideal_min > EPS else None
    if quality is None and total > 0 and FLAG_NO_CYCLE in flags:
        quality = good / total
    oee = good_ideal_min / planned if planned > EPS and FLAG_NO_CYCLE not in flags else None

    if availability is not None and availability > 1 + EPS:
        flags.add(FLAG_AVAILABILITY_OVER)
    if performance is not None and performance > 1 + EPS:
        flags.add(FLAG_PERFORMANCE_OVER)
    if good > total:
        flags.add(FLAG_GOOD_OVER_TOTAL)
    return OEEResult(records, planned, run, total, good, ideal_min, good_ideal_min,
                     availability, performance, quality, oee, frozenset(flags))


def aggregate(rows: Iterable[Mapping]) -> OEEResult:
    """Сводный расчёт по набору записей со ключами
    planned_min, run_min, total_count, good_count, ideal_cycle_sec."""
    records = 0
    planned = run = ideal_min = good_ideal_min = 0.0
    total = good = 0
    flags: set[str] = set()
    for row in rows:
        records += 1
        p, r = _value(row, "planned_min"), _value(row, "run_min")
        t, g = int(_value(row, "total_count")), int(_value(row, "good_count"))
        cycle = _value(row, "ideal_cycle_sec")
        if min(p, r, t, g, cycle) < 0:
            flags.add(FLAG_NEGATIVE)
        if cycle <= EPS:
            flags.add(FLAG_NO_CYCLE)
        planned += p
        run += r
        total += t
        good += g
        ideal_min += t * cycle / 60.0
        good_ideal_min += g * cycle / 60.0
    return _from_totals(records, planned, run, total, good, ideal_min, good_ideal_min, flags)


def calculate(planned_min: float, run_min: float, total_count: int, good_count: int,
              ideal_cycle_sec: float) -> OEEResult:
    """Расчёт для одной записи смены."""
    return aggregate([{
        "planned_min": planned_min, "run_min": run_min, "total_count": total_count,
        "good_count": good_count, "ideal_cycle_sec": ideal_cycle_sec,
    }])


def ratio_status(value: float | None, threshold: float) -> str | None:
    """Статус коэффициента для подсветки: 'bad' ниже порога, 'over' выше 100%."""
    if value is None:
        return None
    if value > 1 + EPS:
        return "over"
    return "bad" if value < threshold else "ok"
