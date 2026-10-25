"""Аналитика за период: сводные показатели, динамика, причины простоев, проверки данных.

Все расчёты используют одни и те же записи, выбранные единым набором фильтров
(``db.Filters``), поэтому KPI, графики, таблицы и CSV-отчёты согласованы.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from . import oee
from .config import SHIFTS, SOURCE_MANUAL
from .db import Database, Filters, parse_ts
from .intervals import Interval, allocate, find_overlaps
from .validation import dates_inclusive

LONG_OPEN_DOWNTIME = timedelta(hours=24)
#: Допуск при сравнении простоев с потерями доступности, мин.
CONSISTENCY_TOLERANCE_MIN = 1.0


def enrich_downtimes(rows: Iterable[dict], now: datetime) -> list[dict]:
    """Добавляет длительность, учтённые без пересечений минуты и признаки состояния.

    Незавершённый простой учитывается до текущего момента ``now``.
    """
    items = []
    for row in rows:
        item = dict(row)
        item["start_dt"] = parse_ts(row["start_at"])
        item["is_open"] = row["end_at"] is None
        end = parse_ts(row["end_at"]) if row["end_at"] else max(now, item["start_dt"])
        item["end_dt"] = end
        item["minutes"] = (end - item["start_dt"]).total_seconds() / 60.0
        items.append(item)
    intervals = [Interval(i["id"], i["equipment_id"], i["start_dt"], i["end_dt"]) for i in items]
    counted = allocate(intervals)
    overlapping = {key for pair in find_overlaps(intervals) for key in (pair[0].key, pair[1].key)}
    for item in items:
        item["counted_min"] = counted.get(item["id"], 0.0)
        item["overlaps"] = item["id"] in overlapping
    return items


@dataclass
class Issue:
    kind: str
    entity: str        # production / downtime
    record_id: int
    text: str


@dataclass
class Snapshot:
    """Данные выбранного периода и производные показатели."""

    filters: Filters
    production: list[dict]
    downtimes: list[dict]
    now: datetime
    _cache: dict = field(default_factory=dict, repr=False)

    # --- Базовые итоги -----------------------------------------------------------

    @property
    def total(self) -> oee.OEEResult:
        if "total" not in self._cache:
            self._cache["total"] = oee.aggregate(self.production)
        return self._cache["total"]

    @property
    def downtime_min(self) -> float:
        return sum(d["counted_min"] for d in self.downtimes)

    @property
    def open_downtimes(self) -> list[dict]:
        return [d for d in self.downtimes if d["is_open"]]

    @property
    def generated_count(self) -> int:
        return (sum(1 for r in self.production if r["source"] != SOURCE_MANUAL)
                + sum(1 for d in self.downtimes if d["source"] != SOURCE_MANUAL))

    @property
    def period(self) -> tuple[date, date] | None:
        """Границы периода; при пустых фильтрах — по фактическим данным."""
        start, end = self.filters.date_from, self.filters.date_to
        dates = [date.fromisoformat(r["work_date"]) for r in self.production]
        dates += [date.fromisoformat(d["work_date"]) for d in self.downtimes]
        if start is None and dates:
            start = min(dates)
        if end is None and dates:
            end = max(dates)
        if start is None or end is None:
            return None
        return start, end

    # --- Разрезы -------------------------------------------------------------------

    def _group(self, key: Callable[[dict], object]) -> dict:
        groups: dict = defaultdict(list)
        for row in self.production:
            groups[key(row)].append(row)
        return {k: oee.aggregate(v) for k, v in groups.items()}

    def daily(self) -> list[tuple[date, oee.OEEResult]]:
        """Результат по каждому дню периода; дни без записей — пустой результат (разрыв на графике)."""
        period = self.period
        if period is None:
            return []
        groups = self._group(lambda r: r["work_date"])
        return [(day, groups.get(day.isoformat(), oee.EMPTY)) for day in dates_inclusive(*period)]

    def by_shift(self) -> list[tuple[str, oee.OEEResult]]:
        groups = self._group(lambda r: r["shift"])
        order = list(SHIFTS) + sorted(k for k in groups if k not in SHIFTS)
        return [(s, groups[s]) for s in order if s in groups]

    def by_equipment(self) -> list[dict]:
        groups = self._group(lambda r: (r["equipment_id"], r["equipment_name"]))
        down: dict = defaultdict(lambda: [0.0, 0])
        names = {}
        for d in self.downtimes:
            down[d["equipment_id"]][0] += d["counted_min"]
            down[d["equipment_id"]][1] += 1
            names[d["equipment_id"]] = d["equipment_name"]
        ids = {k[0] for k in groups} | set(down)
        result = []
        for eq_id in ids:
            name = next((k[1] for k in groups if k[0] == eq_id), names.get(eq_id, "?"))
            res = next((v for k, v in groups.items() if k[0] == eq_id), oee.EMPTY)
            result.append({"equipment_id": eq_id, "equipment_name": name, "result": res,
                           "downtime_min": down[eq_id][0], "downtime_count": down[eq_id][1]})
        result.sort(key=lambda r: (-r["downtime_min"], r["equipment_name"]))
        return result

    def reasons(self) -> list[dict]:
        """Причины простоев по убыванию учтённых минут, с долей и накопленной долей (Парето)."""
        groups: dict = defaultdict(lambda: {"minutes": 0.0, "count": 0, "raw": 0.0})
        for d in self.downtimes:
            g = groups[d["reason"]]
            g["minutes"] += d["counted_min"]
            g["raw"] += d["minutes"]
            g["count"] += 1
        total = sum(g["minutes"] for g in groups.values())
        rows = sorted(({"reason": k, **v} for k, v in groups.items()), key=lambda r: (-r["minutes"], r["reason"]))
        cumulative = 0.0
        for row in rows:
            row["share"] = row["minutes"] / total if total > 0 else None
            cumulative += row["minutes"]
            row["cum_share"] = cumulative / total if total > 0 else None
            row["avg"] = row["raw"] / row["count"] if row["count"] else 0.0
        return rows

    def repeated_problems(self, min_count: int = 2) -> list[dict]:
        """Повторяющиеся причины на одном оборудовании."""
        groups: dict = defaultdict(list)
        for d in self.downtimes:
            groups[(d["equipment_name"], d["reason"])].append(d)
        rows = []
        for (equipment, reason), items in groups.items():
            if len(items) >= min_count:
                minutes = sum(i["counted_min"] for i in items)
                rows.append({"equipment_name": equipment, "reason": reason, "count": len(items),
                             "minutes": minutes, "avg": sum(i["minutes"] for i in items) / len(items),
                             "last": max(i["start_dt"] for i in items),
                             "days": len({i["work_date"] for i in items})})
        rows.sort(key=lambda r: (-r["count"], -r["minutes"]))
        return rows

    def problem_records(self, threshold: float) -> list[dict]:
        """Смены с OEE ниже порога или с предупреждениями расчёта."""
        rows = []
        for row in self.production:
            res = oee.aggregate([row])
            issues = list(res.warnings)
            if res.oee is not None and res.oee < threshold:
                issues.insert(0, f"OEE ниже порога {threshold * 100:.0f}%")
            if issues:
                rows.append({"row": row, "result": res, "issues": issues})
        rows.sort(key=lambda r: (r["result"].oee if r["result"].oee is not None else -1, r["row"]["work_date"]))
        return rows

    def data_issues(self) -> list[Issue]:
        """Проверки согласованности данных периода."""
        issues: list[Issue] = []
        seen: dict = {}
        for row in self.production:
            key = (row["work_date"], row["shift"], row["equipment_id"])
            if key in seen:
                issues.append(Issue("duplicate", "production", row["id"],
                                    f"Повтор записи смены: {row['work_date']}, {row['shift']}, "
                                    f"«{row['equipment_name']}» (также ID {seen[key]})."))
            else:
                seen[key] = row["id"]
            res = oee.aggregate([row])
            for warning in res.warnings:
                issues.append(Issue("calculation", "production", row["id"], warning))
        reported = set()
        for d in self.downtimes:
            if d["overlaps"] and d["id"] not in reported:
                reported.add(d["id"])
                issues.append(Issue("overlap", "downtime", d["id"],
                                    f"Простой пересекается с другим простоем «{d['equipment_name']}»; "
                                    f"учтено без двойного счёта: {d['counted_min']:.0f} из {d['minutes']:.0f} мин."))
            if d["is_open"] and self.now - d["start_dt"] > LONG_OPEN_DOWNTIME:
                issues.append(Issue("open", "downtime", d["id"],
                                    f"Простой «{d['equipment_name']}» не завершён больше суток — проверьте запись."))
        shift_down: dict = defaultdict(float)
        for d in self.downtimes:
            if not d["is_open"]:
                shift_down[(d["work_date"], d["shift"], d["equipment_id"])] += d["counted_min"]
        for row in self.production:
            key = (row["work_date"], row["shift"], row["equipment_id"])
            lost = float(row["planned_min"]) - float(row["run_min"])
            if shift_down.get(key, 0.0) > lost + CONSISTENCY_TOLERANCE_MIN:
                issues.append(Issue("consistency", "production", row["id"],
                                    f"Простои смены ({shift_down[key]:.0f} мин) больше потерь доступности "
                                    f"(план − работа = {lost:.0f} мин): проверьте время работы."))
        return issues


def load_snapshot(db: Database, filters: Filters, now: datetime | None = None) -> Snapshot:
    now = now or datetime.now()
    production = db.list_production(filters)
    downtimes = enrich_downtimes(db.list_downtimes(filters), now)
    return Snapshot(filters, production, downtimes, now)
