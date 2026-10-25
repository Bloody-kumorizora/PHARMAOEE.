"""Экспорт в CSV, совместимый с Excel (UTF-8 с BOM, разделитель «;», десятичная запятая).

В каждый файл записываются период, фильтры, время формирования и
предупреждение, если в выборку попали демонстрационные или симулированные записи.
Значения считаются теми же функциями, что и в интерфейсе.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import APP_NAME, __version__, formatting, oee
from .analytics import Snapshot
from .config import SOURCE_LABELS
from .db import DatabaseError, describe_error

DISCLAIMER = ("Учебный прототип PharmaOEE. Отчёт не является официальной производственной "
              "документацией и не является валидированным GMP-отчётом.")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ExportError(DatabaseError):
    pass


def safe_text(value) -> str:
    """Защита от CSV-инъекций: текст, начинающийся с формульного символа, экранируется."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def num(value: float | None, digits: int = 1) -> str:
    return "" if value is None else f"{value:.{digits}f}".replace(".", ",")


def pct_value(value: float | None) -> str:
    return "" if value is None else num(value * 100, 1)


def _header(writer, title: str, snapshot: Snapshot, filters_text: str) -> None:
    period = snapshot.period
    writer.writerow([f"{APP_NAME} {__version__} — {title}"])
    writer.writerow(["Период (по дате смены, включительно)",
                     f"{formatting.ru_date(period[0])} — {formatting.ru_date(period[1])}" if period else "нет данных"])
    writer.writerow(["Фильтры", filters_text])
    writer.writerow(["Сформирован", formatting.ru_datetime(datetime.now(), with_seconds=True)])
    if snapshot.generated_count:
        writer.writerow(["ВНИМАНИЕ", f"В отчёт включены демонстрационные или симулированные записи: "
                                     f"{snapshot.generated_count}. Это не данные реального производства."])
    writer.writerow([DISCLAIMER])
    writer.writerow([])


def _write(path: Path, rows_writer) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8-sig") as handle:
            rows_writer(csv.writer(handle, delimiter=";"))
        os.replace(tmp, path)
    except PermissionError as exc:
        _cleanup(tmp)
        raise ExportError("Нет доступа к файлу. Если он открыт в Excel, закройте его и повторите.") from exc
    except OSError as exc:
        _cleanup(tmp)
        raise ExportError("Файл не сохранён. " + describe_error(exc)) from exc


def _cleanup(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


PRODUCTION_COLUMNS = ["ID", "Дата смены", "Смена", "Оборудование", "Плановое время, мин", "Время работы, мин",
                      "Произведено, шт.", "Годные, шт.", "Брак, шт.", "Идеальный цикл, с/шт.",
                      "Доступность, %", "Производительность, %", "Качество, %", "OEE, %",
                      "Источник данных", "Предупреждения", "Комментарий"]


def production_rows(rows: Iterable[dict]) -> list[list]:
    result = []
    for r in rows:
        res = oee.aggregate([r])
        result.append([r["id"], formatting.ru_date(r["work_date"]), r["shift"], safe_text(r["equipment_name"]),
                       num(r["planned_min"]), num(r["run_min"]), r["total_count"], r["good_count"],
                       res.defect_count, num(r["ideal_cycle_sec"], 3), pct_value(res.availability),
                       pct_value(res.performance), pct_value(res.quality), pct_value(res.oee),
                       SOURCE_LABELS.get(r["source"], r["source"]), safe_text(" ".join(res.warnings)),
                       safe_text(r["note"])])
    return result


DOWNTIME_COLUMNS = ["ID", "Дата смены", "Смена", "Оборудование", "Начало", "Окончание", "Длительность, мин",
                    "Учтено без пересечений, мин", "Причина", "Комментарий", "Источник данных", "Статус"]


def downtime_rows(rows: Iterable[dict]) -> list[list]:
    result = []
    for d in rows:
        status = "Продолжается" if d["is_open"] else "Завершён"
        if d["overlaps"]:
            status += ", пересекается"
        result.append([d["id"], formatting.ru_date(d["work_date"]), d["shift"], safe_text(d["equipment_name"]),
                       formatting.ru_datetime(d["start_dt"]),
                       "" if d["is_open"] else formatting.ru_datetime(d["end_dt"]),
                       num(d["minutes"]), num(d["counted_min"]), safe_text(d["reason"]), safe_text(d["comment"]),
                       SOURCE_LABELS.get(d["source"], d["source"]), status])
    return result


def export_production(path: Path, snapshot: Snapshot, filters_text: str) -> int:
    def write(w):
        _header(w, "производственные записи", snapshot, filters_text)
        w.writerow(PRODUCTION_COLUMNS)
        w.writerows(production_rows(snapshot.production))
    _write(path, write)
    return len(snapshot.production)


def export_downtimes(path: Path, snapshot: Snapshot, filters_text: str) -> int:
    def write(w):
        _header(w, "журнал простоев", snapshot, filters_text)
        if snapshot.open_downtimes:
            w.writerow(["Незавершённые простои учтены до момента формирования отчёта."])
        w.writerow(DOWNTIME_COLUMNS)
        w.writerows(downtime_rows(snapshot.downtimes))
    _write(path, write)
    return len(snapshot.downtimes)


def _kpi_row(label: str, res: oee.OEEResult) -> list:
    return [label, res.records, num(res.planned_min), num(res.run_min), res.total_count, res.good_count,
            res.defect_count, pct_value(res.availability), pct_value(res.performance),
            pct_value(res.quality), pct_value(res.oee)]


KPI_COLUMNS = ["Срез", "Записей смен", "Плановое время, мин", "Время работы, мин", "Произведено, шт.",
               "Годные, шт.", "Брак, шт.", "Доступность, %", "Производительность, %", "Качество, %", "OEE, %"]


def export_summary(path: Path, snapshot: Snapshot, filters_text: str) -> None:
    def write(w):
        _header(w, "сводный отчёт OEE", snapshot, filters_text)
        total = snapshot.total
        w.writerow(["ИТОГО ЗА ПЕРИОД"])
        w.writerow(KPI_COLUMNS)
        w.writerow(_kpi_row("Весь период", total))
        w.writerow(["Простои (без двойного счёта), мин", num(snapshot.downtime_min),
                    "Записей простоев", len(snapshot.downtimes)])
        for warning in total.warnings:
            w.writerow(["Предупреждение", warning])
        w.writerow([])
        w.writerow(["ПО ДНЯМ"])
        w.writerow(KPI_COLUMNS)
        for day, res in snapshot.daily():
            if res.has_data:
                w.writerow(_kpi_row(formatting.ru_date(day), res))
        w.writerow([])
        w.writerow(["ПО СМЕНАМ"])
        w.writerow(KPI_COLUMNS)
        for shift, res in snapshot.by_shift():
            w.writerow(_kpi_row(shift, res))
        w.writerow([])
        w.writerow(["ПО ОБОРУДОВАНИЮ"])
        w.writerow(KPI_COLUMNS + ["Простои, мин", "Число простоев"])
        for item in snapshot.by_equipment():
            w.writerow(_kpi_row(safe_text(item["equipment_name"]), item["result"])
                       + [num(item["downtime_min"]), item["downtime_count"]])
        w.writerow([])
        w.writerow(["ПРИЧИНЫ ПРОСТОЕВ"])
        w.writerow(["Причина", "Учтено, мин", "Доля, %", "Накопленная доля, %", "Число простоев",
                    "Средняя длительность, мин"])
        for r in snapshot.reasons():
            w.writerow([safe_text(r["reason"]), num(r["minutes"]), pct_value(r["share"]), pct_value(r["cum_share"]),
                        r["count"], num(r["avg"])])
    _write(path, write)
