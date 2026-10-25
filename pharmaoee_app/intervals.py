"""Работа с интервалами времени простоев.

Правило пересечений (действует во всём приложении):

1. Для одного оборудования новые простои не могут пересекаться с уже
   сохранёнными — форма блокирует сохранение и показывает конфликт.
2. Если пересечения всё же есть (старые данные, симулятор), общая
   длительность считается по объединению интервалов: одно и то же время
   не учитывается дважды.
3. Пересекающаяся часть относится к простою, который начался раньше
   (при равном начале — к записи с меньшим ID). Поэтому сумма «учтённых»
   минут по причинам равна общей длительности без двойного счёта.
4. Простои разных единиц оборудования не сравниваются между собой.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Hashable, Iterable


@dataclass(frozen=True)
class Interval:
    key: Hashable          # идентификатор записи (ID простоя)
    group: Hashable        # оборудование: пересечения ищутся только внутри группы
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 60.0)


def duration_min(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Интервалы [a_start, a_end) и [b_start, b_end) пересекаются (касание — не пересечение)."""
    return a_start < b_end and b_start < a_end


def _sorted(items: Iterable[Interval]) -> list[Interval]:
    def order(i: Interval):
        numeric = i.key if isinstance(i.key, (int, float)) else 0
        return (str(i.group), i.start, numeric, str(i.key))
    return sorted(items, key=order)


def allocate(items: Iterable[Interval]) -> dict[Hashable, float]:
    """Учтённые минуты каждой записи без двойного счёта (правило 3)."""
    result: dict[Hashable, float] = {}
    covered_until: dict[Hashable, datetime] = {}
    for item in _sorted(items):
        edge = covered_until.get(item.group)
        start = item.start if edge is None or item.start >= edge else edge
        result[item.key] = max(0.0, duration_min(start, item.end)) if item.end > start else 0.0
        if edge is None or item.end > edge:
            covered_until[item.group] = item.end
    return result


def union_minutes(items: Iterable[Interval]) -> float:
    return sum(allocate(items).values())


def find_overlaps(items: Iterable[Interval]) -> list[tuple[Interval, Interval]]:
    """Пары пересекающихся интервалов внутри одной группы."""
    pairs: list[tuple[Interval, Interval]] = []
    active: dict[Hashable, list[Interval]] = {}
    for item in _sorted(items):
        bucket = [other for other in active.get(item.group, []) if other.end > item.start]
        for other in bucket:
            if overlaps(other.start, other.end, item.start, item.end):
                pairs.append((other, item))
        bucket.append(item)
        active[item.group] = bucket
    return pairs
