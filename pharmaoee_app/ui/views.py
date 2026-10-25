"""Основные разделы: главная панель, производство, оборудование, простои, аналитика."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .. import export, formatting, oee
from ..config import SOURCE_LABELS, SOURCE_MANUAL
from ..db import DatabaseError, describe_error
from . import theme
from .charts import Bar, GroupedBarChart, HBarChart, LineChart, Series, StackedBarChart
from .dialogs import (CloseDowntimeDialog, DowntimeDialog, EquipmentDialog, ProductionDialog, ReasonsDialog)
from .theme import C, METRIC_COLORS
from .widgets import Banner, Card, Column, DataTable, KpiTile, MetricStrip, ScrollableFrame, Tooltip

HELP = {
    "oee": "OEE (Overall Equipment Effectiveness) — общая эффективность оборудования.\n"
           "OEE = Доступность × Производительность × Качество = годные × идеальный цикл / плановое время.",
    "availability": "Доступность (Availability) = время работы / плановое время.\n"
                    "Показывает потери из-за остановок.",
    "performance": "Производительность (Performance) = произведено × идеальный цикл / время работы.\n"
                   "Показывает потери скорости. Выше 100% — повод проверить идеальный цикл и исходные данные.",
    "quality": "Качество (Quality) = годные / произведено.\n"
               "Для нескольких записей с разным циклом взвешивается идеальным временем выпуска.",
    "planned": "Сумма планового производственного времени записей смен за период.",
    "run": "Сумма фактического времени работы записей смен.",
    "downtime": "Зарегистрированные простои по журналу (по дате смены), без двойного учёта пересечений. "
                "Незавершённые простои учитываются до текущего момента.",
    "total": "Всего произведено единиц по записям смен.",
    "good": "Годные единицы по записям смен.",
    "defects": "Брак = произведено − годные. В скобках — доля брака.",
}


def fmt_pct(v):
    return formatting.pct(v)


def fmt_min(v):
    return formatting.number(v, 0) if v is not None else "—"


def fmt_int(v):
    return formatting.integer(v)


def fmt_date(v):
    return formatting.ru_date(v)


class View(ttk.Frame):
    title = ""
    subtitle = ""
    uses_filters = True

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.stale = True

    def on_show(self):
        if self.stale:
            self.refresh()

    def refresh(self):
        self.stale = False

    def toolbar(self, parent=None) -> tuple[tk.Frame, tk.Frame]:
        bar = tk.Frame(parent or self, bg=C["bg"])
        bar.pack(fill="x", pady=(0, 10))
        right = tk.Frame(bar, bg=C["bg"])
        right.pack(side="right")
        return bar, right

    def search_box(self, parent, table: DataTable) -> ttk.Entry:
        var = tk.StringVar()
        tk.Label(parent, text="Поиск:", bg=C["bg"], fg=C["ink2"], font=theme.FONTS.small).pack(side="left", padx=(0, 4))
        entry = ttk.Entry(parent, textvariable=var, width=24)
        entry.pack(side="left")
        var.trace_add("write", lambda *_: table.search(var.get()))
        Tooltip(entry, "Поиск по всем столбцам загруженных строк")
        return entry

    def guard(self, func, *args):
        """Выполняет действие с данными и показывает понятную ошибку."""
        try:
            return func(*args)
        except DatabaseError as exc:
            messagebox.showerror("Не удалось выполнить действие", describe_error(exc), parent=self.app.root)
        except Exception as exc:     # noqa: BLE001
            self.app.log_exception(exc)
            messagebox.showerror("Ошибка", describe_error(exc), parent=self.app.root)
        return None


def result_fields(res: oee.OEEResult) -> dict:
    return {"availability": res.availability, "performance": res.performance, "quality": res.quality,
            "oee": res.oee, "defects": res.defect_count}


def result_tags(row: dict) -> tuple:
    tags = []
    if row.get("warnings"):
        tags.append("warn")
    if row.get("source") and row["source"] != SOURCE_MANUAL:
        tags.append("generated")
    return tuple(tags)


# --- Главная панель -------------------------------------------------------------------------

class DashboardView(View):
    title = "Главная панель"
    subtitle = "Ключевые показатели за выбранный период по сохранённым данным."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body
        self.banner = Banner(body)
        self.kpi_row = ttk.Frame(body)
        self.kpi_row.pack(fill="x", pady=(0, theme.GAP))
        self.tiles = {
            "oee": KpiTile(self.kpi_row, "OEE — общая эффективность", HELP["oee"], hero=True),
            "availability": KpiTile(self.kpi_row, "Доступность (Availability)", HELP["availability"]),
            "performance": KpiTile(self.kpi_row, "Производительность (Performance)", HELP["performance"]),
            "quality": KpiTile(self.kpi_row, "Качество (Quality)", HELP["quality"]),
        }
        self.metrics = MetricStrip(body, [
            ("planned", "Плановое время", HELP["planned"]), ("run", "Время работы", HELP["run"]),
            ("downtime", "Простои по журналу", HELP["downtime"]), ("total", "Произведено, шт.", HELP["total"]),
            ("good", "Годные, шт.", HELP["good"]), ("defects", "Брак, шт.", HELP["defects"]),
        ])
        self.metrics.pack(fill="x", pady=(0, theme.GAP))
        self.charts_row = ttk.Frame(body)
        self.charts_row.pack(fill="x", pady=(0, theme.GAP))
        self.trend_card = Card(self.charts_row, "Динамика OEE по дням",
                               "Разрывы линии — дни без записей смен. Пунктир — порог из настроек.")
        self.trend = LineChart(self.trend_card.body, height=250)
        self.trend.pack(fill="both", expand=True)
        self.reasons_card = Card(self.charts_row, "Главные причины простоев", "Минуты без двойного учёта")
        self.reasons_chart = HBarChart(self.reasons_card.body, color=theme.SERIES[1], height=250,
                                       empty_text="Простоев за период нет")
        self.reasons_chart.pack(fill="both", expand=True)
        self.attention = Card(body, "Требует внимания", "Незавершённые простои, предупреждения расчёта и "
                                                        "несогласованные данные за период")
        self.attention.pack(fill="x")
        ttk.Button(self.attention.actions, text="Все проверки →",
                   command=lambda: app.show_view("analytics", tab="issues")).pack()
        self.attention_list = tk.Frame(self.attention.body, bg=C["surface"])
        self.attention_list.pack(fill="x")
        self._layout = None
        self.bind("<Configure>", self._relayout)

    def _relayout(self, _event=None):
        wide = self.winfo_width() >= 1050
        if wide == self._layout:
            return
        self._layout = wide
        for tile in self.tiles.values():
            tile.grid_forget()
        tiles = list(self.tiles.values())
        if wide:
            for i, tile in enumerate(tiles):
                tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else theme.GAP / 2, 0))
                self.kpi_row.columnconfigure(i, weight=2 if i == 0 else 1, uniform="")
        else:
            for i, tile in enumerate(tiles):
                tile.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0 if i % 2 == 0 else theme.GAP / 2, 0),
                          pady=(0, theme.GAP / 2))
            for i in range(4):
                self.kpi_row.columnconfigure(i, weight=1 if i < 2 else 0)
        self.trend_card.grid_forget()
        self.reasons_card.grid_forget()
        if wide:
            self.trend_card.grid(row=0, column=0, sticky="nsew", padx=(0, theme.GAP / 2))
            self.reasons_card.grid(row=0, column=1, sticky="nsew", padx=(theme.GAP / 2, 0))
            self.charts_row.columnconfigure(0, weight=3)
            self.charts_row.columnconfigure(1, weight=2)
        else:
            self.trend_card.grid(row=0, column=0, sticky="nsew", pady=(0, theme.GAP))
            self.reasons_card.grid(row=1, column=0, sticky="nsew")
            self.charts_row.columnconfigure(0, weight=1)
            self.charts_row.columnconfigure(1, weight=0)

    def refresh(self):
        super().refresh()
        snap = self.app.snapshot()
        total = snap.total
        threshold = self.app.settings.oee_threshold_pct / 100
        if total.has_data:
            self.tiles["oee"].set(fmt_pct(total.oee), f"{total.records} записей смен · порог {threshold * 100:.0f}%",
                                  oee.ratio_status(total.oee, threshold))
            for key in ("availability", "performance", "quality"):
                value = getattr(total, key)
                status = "over" if value is not None and value > 1 + oee.EPS else None
                sub = "выше 100% — проверьте данные" if status else ""
                if key == "quality" and total.good_share is not None and value is not None \
                        and abs(total.good_share - value) > 0.0005:
                    sub = f"доля годных по штукам: {fmt_pct(total.good_share)}"
                self.tiles[key].set(fmt_pct(value), sub, status)
        else:
            for key, tile in self.tiles.items():
                tile.set("—", "нет записей смен за период" if key == "oee" else "")
        m = self.metrics
        m.set("planned", formatting.minutes(total.planned_min) if total.has_data else "—")
        m.set("run", formatting.minutes(total.run_min) if total.has_data else "—")
        m.set("downtime", formatting.minutes(snap.downtime_min) if snap.downtimes else "0 мин")
        m.set("total", fmt_int(total.total_count) if total.has_data else "—")
        m.set("good", fmt_int(total.good_count) if total.has_data else "—")
        share = (f" ({fmt_pct(1 - total.good_share)})" if total.good_share is not None else "")
        m.set("defects", (fmt_int(total.defect_count) + share) if total.has_data else "—",
              C["bad"] if total.has_data and total.defect_count < 0 else None)

        daily = snap.daily()
        self.trend.set_data([d for d, _ in daily], [Series("OEE", METRIC_COLORS["oee"], [r.oee for _, r in daily])],
                            reference=(threshold, f"порог {threshold * 100:.0f}%"))
        reasons = snap.reasons()[:6]
        self.reasons_chart.set_data([Bar(r["reason"], r["minutes"], formatting.minutes(r["minutes"]),
                                         f"{r['reason']}\n{formatting.minutes(r['minutes'])} · "
                                         f"{fmt_pct(r['share'])} · {r['count']} простоев") for r in reasons])
        self._banner(snap)
        self._attention(snap)

    def _banner(self, snap):
        messages, kind = [], "info"
        if not snap.production and not snap.downtimes:
            messages.append("За выбранный период нет записей. Измените период в фильтре сверху, добавьте запись "
                            "смены в разделе «Производство» или создайте демо-данные в разделе «Демо и симулятор».")
        if snap.generated_count:
            messages.append(f"В показатели включены демонстрационные или симулированные записи: "
                            f"{snap.generated_count}. Чтобы видеть только ручной ввод, выберите «Только ручной ввод» "
                            "в фильтре «Данные».")
            kind = "warn"
        if messages:
            self.banner.set("\n".join(messages), kind)
            self.banner.pack(fill="x", pady=(0, theme.GAP), before=self.kpi_row)
        else:
            self.banner.pack_forget()

    def _attention(self, snap):
        for child in self.attention_list.winfo_children():
            child.destroy()
        items = []
        for d in snap.open_downtimes:
            items.append(("warn", f"Простой продолжается: «{d['equipment_name']}» с "
                                  f"{formatting.ru_datetime(d['start_dt'])} ({formatting.minutes(d['minutes'])}), "
                                  f"причина: {d['reason']}."))
        for issue in snap.data_issues():
            if issue.kind != "open":
                items.append(("bad" if issue.kind in ("duplicate", "overlap") else "warn", issue.text))
        unclassified = [d for d in snap.downtimes if d["reason"] == "Не классифицировано"]
        if unclassified:
            items.append(("info", f"Простоев без причины («Не классифицировано»): {len(unclassified)}. "
                                  "Укажите причину в разделе «Простои»."))
        if not items:
            tk.Label(self.attention_list, text="Замечаний по данным периода нет.", bg=C["surface"], fg=C["muted"],
                     font=theme.FONTS.body, anchor="w").pack(fill="x")
            return
        colors = {"warn": C["warn"], "bad": C["bad"], "info": C["primary"]}
        for kind, text in items[:8]:
            row = tk.Frame(self.attention_list, bg=C["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text="●", bg=C["surface"], fg=colors[kind], font=theme.FONTS.small).pack(side="left",
                                                                                                    anchor="n")
            label = tk.Label(row, text=text, bg=C["surface"], fg=C["ink"], font=theme.FONTS.body, anchor="w",
                             justify="left")
            label.pack(side="left", fill="x", expand=True, padx=(6, 0))
            label.bind("<Configure>", lambda e, l=label: l.configure(wraplength=max(200, e.width - 10)))
        if len(items) > 8:
            tk.Label(self.attention_list, text=f"…и ещё {len(items) - 8}", bg=C["surface"], fg=C["muted"],
                     font=theme.FONTS.small, anchor="w").pack(fill="x", pady=(4, 0))


# --- Производство -------------------------------------------------------------------------------

class ProductionView(View):
    title = "Производственные показатели"
    subtitle = "Записи смен за период: время, выпуск и рассчитанные коэффициенты OEE."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar, right = self.toolbar()
        ttk.Button(bar, text="+ Добавить запись", style="Primary.TButton", command=self.add).pack(side="left")
        ttk.Button(bar, text="Изменить", command=self.edit).pack(side="left", padx=6)
        ttk.Button(bar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="left")
        card = Card(self, padding=0)
        card.pack(fill="both", expand=True)
        self.table = DataTable(card.body, [
            Column("work_date", "Дата", 84, fmt=fmt_date),
            Column("shift", "Смена", 64),
            Column("equipment_name", "Оборудование", 190, stretch=True),
            Column("planned_min", "План, мин", 78, "e", numeric=True, fmt=fmt_min),
            Column("run_min", "Работа, мин", 88, "e", numeric=True, fmt=fmt_min),
            Column("total_count", "Произв., шт.", 92, "e", numeric=True, fmt=fmt_int),
            Column("good_count", "Годные, шт.", 90, "e", numeric=True, fmt=fmt_int),
            Column("defects", "Брак, шт.", 74, "e", numeric=True, fmt=fmt_int),
            Column("ideal_cycle_sec", "Цикл, с", 62, "e", numeric=True, fmt=lambda v: formatting.number(v, 2)),
            Column("availability", "A", 60, "e", numeric=True, fmt=fmt_pct),
            Column("performance", "P", 60, "e", numeric=True, fmt=fmt_pct),
            Column("quality", "Q", 60, "e", numeric=True, fmt=fmt_pct),
            Column("oee", "OEE", 62, "e", numeric=True, fmt=fmt_pct),
            Column("source_label", "Источник", 86),
            Column("note", "Комментарий", 150),
        ], height=16, empty_text="Нет записей смен за выбранный период.\nНажмите «+ Добавить запись».")
        self.table.pack(fill="both", expand=True)
        self.table.set_tags(result_tags)
        self.table.bind_open(self.edit)
        self.search_box(right, self.table)
        self.footer = tk.Label(self, text="", bg=C["bg"], fg=C["ink2"], font=theme.FONTS.small, anchor="w",
                               justify="left")
        self.footer.pack(fill="x", pady=(8, 0))

    def refresh(self):
        super().refresh()
        snap = self.app.snapshot()
        rows = []
        for r in snap.production:
            res = oee.aggregate([r])
            rows.append({**r, **result_fields(res), "warnings": res.warnings,
                         "source_label": SOURCE_LABELS.get(r["source"], r["source"])})
        self.table.set_rows(rows)
        total = snap.total
        if total.has_data:
            self.footer.configure(text=f"Итого по {total.records} записям: OEE {fmt_pct(total.oee)} · доступность "
                                       f"{fmt_pct(total.availability)} · производительность "
                                       f"{fmt_pct(total.performance)} · качество {fmt_pct(total.quality)} · "
                                       f"выпуск {fmt_int(total.total_count)} шт. Строки жёлтым — есть "
                                       "предупреждения расчёта; серым — демо/симулятор. Двойной щелчок — изменить.")
        else:
            self.footer.configure(text="")

    def add(self):
        f = self.app.filters()
        defaults = {"equipment_id": f.equipment_id, "shift": f.shift,
                    "work_date": f.date_to if f.date_to and f.date_to == f.date_from else None}
        self.guard(lambda: ProductionDialog(self.app, defaults=defaults).show())

    def edit(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Изменение записи", "Выберите запись в таблице.", parent=self.app.root)
            return
        self.guard(lambda: ProductionDialog(self.app, record=self.app.db.get_production(row["id"])).show())

    def delete(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Удаление записи", "Выберите запись в таблице.", parent=self.app.root)
            return
        text = (f"Удалить запись смены?\n\n{fmt_date(row['work_date'])}, {row['shift']}, «{row['equipment_name']}»\n"
                f"Произведено: {fmt_int(row['total_count'])} шт., OEE {fmt_pct(row['oee'])}\n\n"
                "Действие нельзя отменить (кроме восстановления из резервной копии). "
                "Удаление фиксируется в журнале изменений.")
        if messagebox.askyesno("Удаление записи", text, icon="warning", parent=self.app.root, default="no"):
            if self.guard(lambda: (self.app.db.delete_production(row["id"]), True)[1]):
                self.app.data_changed("Запись смены удалена.")


# --- Оборудование ------------------------------------------------------------------------------

class EquipmentView(View):
    title = "Оборудование"
    subtitle = "Справочник линий и единиц оборудования. Показатели — за выбранный период."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar, right = self.toolbar()
        ttk.Button(bar, text="+ Добавить", style="Primary.TButton", command=self.add).pack(side="left")
        ttk.Button(bar, text="Изменить", command=self.edit).pack(side="left", padx=6)
        ttk.Button(bar, text="Используется / нет", command=self.toggle).pack(side="left")
        ttk.Button(bar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="left", padx=6)
        card = Card(self, padding=0)
        card.pack(fill="both", expand=True)
        self.table = DataTable(card.body, [
            Column("name", "Название", 240, stretch=True),
            Column("area", "Участок", 120),
            Column("ideal_cycle_sec", "Цикл, с/шт.", 90, "e", numeric=True, fmt=lambda v: formatting.number(v, 2)),
            Column("status", "Статус", 110),
            Column("kind", "Тип", 90),
            Column("oee", "OEE за период", 110, "e", numeric=True, fmt=fmt_pct),
            Column("downtime_min", "Простои, мин", 100, "e", numeric=True, fmt=fmt_min),
            Column("production_count", "Смен всего", 90, "e", numeric=True),
            Column("downtime_count", "Простоев всего", 110, "e", numeric=True),
            Column("note", "Примечание", 180),
        ], height=16, empty_text="Справочник пуст. Добавьте оборудование кнопкой «+ Добавить».")
        self.table.pack(fill="both", expand=True)
        self.table.set_tags(lambda r: ("muted",) if not r["is_active"] else (("generated",) if r["is_demo"] else ()))
        self.table.bind_open(self.edit)
        self.search_box(right, self.table)
        tk.Label(self, text="Оборудование, по которому есть записи, нельзя удалить — снимите отметку «Используется», "
                            "чтобы скрыть его из новых форм и сохранить историю.",
                 bg=C["bg"], fg=C["muted"], font=theme.FONTS.small, anchor="w").pack(fill="x", pady=(8, 0))

    def refresh(self):
        super().refresh()
        period = {item["equipment_id"]: item for item in self.app.snapshot().by_equipment()}
        rows = []
        for e in self.app.db.list_equipment():
            stats = period.get(e["id"])
            rows.append({**e, "status": "используется" if e["is_active"] else "не используется",
                         "kind": "ДЕМО" if e["is_demo"] else "реальное",
                         "oee": stats["result"].oee if stats else None,
                         "downtime_min": stats["downtime_min"] if stats else None})
        self.table.set_rows(rows)

    def _selected(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Оборудование", "Выберите оборудование в таблице.", parent=self.app.root)
        return row

    def add(self):
        self.guard(lambda: EquipmentDialog(self.app).show())

    def edit(self):
        row = self._selected()
        if row:
            self.guard(lambda: EquipmentDialog(self.app, self.app.db.get_equipment(row["id"])).show())

    def toggle(self):
        row = self._selected()
        if not row:
            return
        from ..validation import EquipmentInput
        data = EquipmentInput(row["name"], row["area"], row["ideal_cycle_sec"], row["note"], not row["is_active"])
        if self.guard(lambda: (self.app.db.update_equipment(row["id"], data), True)[1]):
            self.app.data_changed("Статус оборудования изменён.")

    def delete(self):
        row = self._selected()
        if not row:
            return
        if messagebox.askyesno("Удаление оборудования", f"Удалить «{row['name']}» из справочника?",
                               icon="warning", parent=self.app.root, default="no"):
            if self.guard(lambda: (self.app.db.delete_equipment(row["id"]), True)[1]):
                self.app.data_changed("Оборудование удалено.")


# --- Простои ---------------------------------------------------------------------------------------

class DowntimeView(View):
    title = "Простои"
    subtitle = "Регистрация, классификация и анализ остановок. Период фильтруется по дате смены."

    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar, right = self.toolbar()
        ttk.Button(bar, text="+ Добавить простой", style="Primary.TButton", command=self.add).pack(side="left")
        ttk.Button(bar, text="Изменить", command=self.edit).pack(side="left", padx=6)
        ttk.Button(bar, text="Завершить", command=self.close).pack(side="left")
        ttk.Button(bar, text="Удалить", style="Danger.TButton", command=self.delete).pack(side="left", padx=6)
        ttk.Button(bar, text="Справочник причин…", command=lambda: ReasonsDialog(app)).pack(side="left")
        self.banner = Banner(self)
        self.content = ttk.Frame(self)
        self.content.pack(fill="both", expand=True)
        card = Card(self.content, padding=0)
        card.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, theme.GAP))
        self.table = DataTable(card.body, [
            Column("work_date", "Дата смены", 84, fmt=fmt_date),
            Column("shift", "Смена", 64),
            Column("equipment_name", "Оборудование", 190, stretch=True),
            Column("start_at", "Начало", 112, fmt=lambda v: formatting.ru_datetime(v)),
            Column("end_text", "Окончание", 112),
            Column("minutes", "Длит., мин", 74, "e", numeric=True, fmt=fmt_min),
            Column("counted_min", "Учтено, мин", 80, "e", numeric=True, fmt=fmt_min),
            Column("reason", "Причина", 170),
            Column("comment", "Комментарий", 180),
            Column("source_label", "Источник", 84),
        ], height=12, empty_text="Простоев за выбранный период нет.")
        self.table.pack(fill="both", expand=True)
        self.table.set_tags(self._tags)
        self.table.bind_open(self.edit)
        self.search_box(right, self.table)
        Tooltip(card, "«Учтено» — минуты без двойного учёта пересечений. Жёлтым — незавершённые, "
                      "красным — пересекающиеся простои.")
        reasons_card = Card(self.content, "По причинам")
        reasons_card.grid(row=1, column=0, sticky="nsew", padx=(0, theme.GAP / 2))
        self.reasons = DataTable(reasons_card.body, [
            Column("reason", "Причина", 180, stretch=True),
            Column("minutes", "Мин", 70, "e", numeric=True, fmt=fmt_min),
            Column("share", "Доля", 64, "e", numeric=True, fmt=fmt_pct),
            Column("count", "Кол-во", 64, "e", numeric=True),
            Column("avg", "Средн., мин", 84, "e", numeric=True, fmt=fmt_min),
        ], height=6, key="reason", empty_text="Нет данных")
        self.reasons.pack(fill="both", expand=True)
        eq_card = Card(self.content, "По оборудованию")
        eq_card.grid(row=1, column=1, sticky="nsew", padx=(theme.GAP / 2, 0))
        self.by_equipment = DataTable(eq_card.body, [
            Column("equipment_name", "Оборудование", 180, stretch=True),
            Column("downtime_min", "Мин", 70, "e", numeric=True, fmt=fmt_min),
            Column("downtime_count", "Кол-во", 64, "e", numeric=True),
        ], height=6, key="equipment_id", empty_text="Нет данных")
        self.by_equipment.pack(fill="both", expand=True)
        self.content.rowconfigure(0, weight=3)
        self.content.rowconfigure(1, weight=1)
        self.content.columnconfigure(0, weight=3, uniform="d")
        self.content.columnconfigure(1, weight=2, uniform="d")

    @staticmethod
    def _tags(row):
        tags = []
        if row["overlaps"]:
            tags.append("bad")
        elif row["is_open"]:
            tags.append("warn")
        if row["source"] != SOURCE_MANUAL:
            tags.append("generated")
        return tuple(tags)

    def refresh(self):
        super().refresh()
        snap = self.app.snapshot()
        rows = [{**d, "end_text": "продолжается" if d["is_open"] else formatting.ru_datetime(d["end_dt"]),
                 "source_label": SOURCE_LABELS.get(d["source"], d["source"])} for d in snap.downtimes]
        self.table.set_rows(rows)
        self.reasons.set_rows(snap.reasons())
        self.by_equipment.set_rows([e for e in snap.by_equipment() if e["downtime_count"]])
        messages = [f"Всего: {len(rows)} простоев, {formatting.minutes(snap.downtime_min)} без двойного учёта."]
        kind = "info"
        if snap.open_downtimes:
            messages.append(f"Продолжаются: {len(snap.open_downtimes)} — выберите строку и нажмите «Завершить».")
            kind = "warn"
        overlaps = sum(1 for d in snap.downtimes if d["overlaps"])
        if overlaps:
            messages.append(f"Пересекающихся записей: {overlaps}; общее время посчитано по объединению интервалов.")
            kind = "warn"
        self.banner.set(" ".join(messages), kind)
        self.banner.pack(fill="x", pady=(0, theme.GAP), before=self.content)

    def _selected(self, action: str):
        row = self.table.selected()
        if not row:
            messagebox.showinfo(action, "Выберите простой в таблице.", parent=self.app.root)
        return row

    def add(self):
        f = self.app.filters()
        self.guard(lambda: DowntimeDialog(self.app, defaults={"equipment_id": f.equipment_id}).show())

    def edit(self):
        row = self._selected("Изменение простоя")
        if row:
            self.guard(lambda: DowntimeDialog(self.app, record=self.app.db.get_downtime(row["id"])).show())

    def close(self):
        row = self._selected("Завершение простоя")
        if not row:
            return
        if not row["is_open"]:
            messagebox.showinfo("Завершение простоя", "Этот простой уже завершён.", parent=self.app.root)
            return
        self.guard(lambda: CloseDowntimeDialog(self.app, self.app.db.get_downtime(row["id"])).show())

    def delete(self):
        row = self._selected("Удаление простоя")
        if not row:
            return
        text = (f"Удалить простой?\n\n«{row['equipment_name']}», {formatting.ru_datetime(row['start_dt'])}, "
                f"{formatting.minutes(row['minutes'])}, причина: {row['reason']}\n\n"
                "Удаление фиксируется в журнале изменений.")
        if messagebox.askyesno("Удаление простоя", text, icon="warning", parent=self.app.root, default="no"):
            if self.guard(lambda: (self.app.db.delete_downtime(row["id"]), True)[1]):
                self.app.data_changed("Простой удалён.")


# --- Аналитика и отчёты -----------------------------------------------------------------------------

class AnalyticsView(View):
    title = "Аналитика и отчёты"
    subtitle = "Графики и таблицы за выбранный период; экспорт использует те же фильтры и формулы."

    TABS = ("trend", "reasons", "equipment", "shifts", "output", "problems", "repeated", "issues")

    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar, right = self.toolbar()
        tk.Label(bar, text="Экспорт CSV (Excel):", bg=C["bg"], fg=C["ink2"], font=theme.FONTS.small).pack(side="left")
        ttk.Button(bar, text="Сводный отчёт", style="Primary.TButton",
                   command=lambda: self.export("summary")).pack(side="left", padx=6)
        ttk.Button(bar, text="Записи смен", command=lambda: self.export("production")).pack(side="left")
        ttk.Button(bar, text="Простои", command=lambda: self.export("downtimes")).pack(side="left", padx=6)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        self.tabs: dict[str, ttk.Frame] = {}
        names = {"trend": "Динамика OEE", "reasons": "Причины простоев", "equipment": "Оборудование",
                 "shifts": "Смены", "output": "Выпуск и брак", "problems": "Проблемные смены",
                 "repeated": "Повторяющиеся проблемы", "issues": "Проверка данных"}
        for key in self.TABS:
            frame = tk.Frame(self.notebook, bg=C["surface"], padx=14, pady=12)
            self.notebook.add(frame, text=names[key])
            self.tabs[key] = frame
        self._build_trend()
        self._build_reasons()
        self._build_equipment()
        self._build_shifts()
        self._build_output()
        self._build_problems()
        self._build_repeated()
        self._build_issues()

    def select_tab(self, key: str):
        if key in self.tabs:
            self.notebook.select(self.tabs[key])

    def _build_trend(self):
        f = self.tabs["trend"]
        controls = tk.Frame(f, bg=C["surface"])
        controls.pack(fill="x")
        tk.Label(controls, text="Показатели:", bg=C["surface"], fg=C["ink2"], font=theme.FONTS.small).pack(side="left")
        self.trend_vars = {}
        for key, name in (("oee", "OEE"), ("availability", "Доступность"), ("performance", "Производительность"),
                          ("quality", "Качество")):
            var = tk.BooleanVar(value=True)
            self.trend_vars[key] = (var, name)
            ttk.Checkbutton(controls, text=name, variable=var, style="Card.TCheckbutton",
                            command=self._draw_trend).pack(side="left", padx=6)
        self.trend_chart = LineChart(f, height=260)
        self.trend_chart.pack(fill="both", expand=True, pady=(6, 8))
        self.trend_table = DataTable(f, [
            Column("day", "Дата", 90, fmt=fmt_date),
            Column("records", "Смен", 60, "e", numeric=True),
            Column("availability", "Доступность", 96, "e", numeric=True, fmt=fmt_pct),
            Column("performance", "Производит.", 96, "e", numeric=True, fmt=fmt_pct),
            Column("quality", "Качество", 86, "e", numeric=True, fmt=fmt_pct),
            Column("oee", "OEE", 76, "e", numeric=True, fmt=fmt_pct),
            Column("total", "Произведено", 100, "e", numeric=True, fmt=fmt_int),
            Column("downtime", "Простои, мин", 100, "e", numeric=True, fmt=fmt_min),
        ], height=7, key="day", empty_text="Нет данных за период")
        self.trend_table.pack(fill="both", expand=True)

    def _draw_trend(self):
        daily = getattr(self, "_daily", [])
        series = [Series(name, METRIC_COLORS[key], [getattr(r, key) for _, r in daily])
                  for key, (var, name) in self.trend_vars.items() if var.get()]
        threshold = self.app.settings.oee_threshold_pct / 100
        self.trend_chart.set_data([d for d, _ in daily], series, reference=(threshold, f"порог OEE {threshold * 100:.0f}%"))

    def _build_reasons(self):
        f = self.tabs["reasons"]
        tk.Label(f, text="Парето причин: какие причины дают основную часть потерянного времени.", bg=C["surface"],
                 fg=C["muted"], font=theme.FONTS.small, anchor="w").pack(fill="x")
        self.reasons_chart = HBarChart(f, color=theme.SERIES[1], height=260, empty_text="Простоев за период нет")
        self.reasons_chart.pack(fill="both", expand=True, pady=(6, 8))
        self.reasons_table = DataTable(f, [
            Column("reason", "Причина", 220, stretch=True),
            Column("minutes", "Учтено, мин", 96, "e", numeric=True, fmt=fmt_min),
            Column("share", "Доля", 70, "e", numeric=True, fmt=fmt_pct),
            Column("cum_share", "Накопл. доля", 96, "e", numeric=True, fmt=fmt_pct),
            Column("count", "Простоев", 76, "e", numeric=True),
            Column("avg", "Средняя длит., мин", 130, "e", numeric=True, fmt=fmt_min),
        ], height=7, key="reason", empty_text="Нет данных")
        self.reasons_table.pack(fill="both", expand=True)

    def _build_equipment(self):
        f = self.tabs["equipment"]
        tk.Label(f, text="Рейтинг оборудования по продолжительности простоев.", bg=C["surface"], fg=C["muted"],
                 font=theme.FONTS.small, anchor="w").pack(fill="x")
        self.equipment_chart = HBarChart(f, color=theme.SERIES[0], height=220, empty_text="Простоев за период нет")
        self.equipment_chart.pack(fill="both", expand=True, pady=(6, 8))
        self.equipment_table = DataTable(f, [
            Column("equipment_name", "Оборудование", 220, stretch=True),
            Column("downtime_min", "Простои, мин", 100, "e", numeric=True, fmt=fmt_min),
            Column("downtime_count", "Простоев", 80, "e", numeric=True),
            Column("availability", "Доступность", 96, "e", numeric=True, fmt=fmt_pct),
            Column("performance", "Производит.", 96, "e", numeric=True, fmt=fmt_pct),
            Column("quality", "Качество", 86, "e", numeric=True, fmt=fmt_pct),
            Column("oee", "OEE", 76, "e", numeric=True, fmt=fmt_pct),
            Column("records", "Смен", 60, "e", numeric=True),
        ], height=7, key="equipment_id", empty_text="Нет данных")
        self.equipment_table.pack(fill="both", expand=True)

    def _build_shifts(self):
        f = self.tabs["shifts"]
        tk.Label(f, text="Сравнение смен по составляющим OEE.", bg=C["surface"], fg=C["muted"],
                 font=theme.FONTS.small, anchor="w").pack(fill="x")
        self.shift_chart = GroupedBarChart(f, height=260, empty_text="Нет записей смен за период")
        self.shift_chart.pack(fill="both", expand=True, pady=(6, 8))
        self.shift_table = DataTable(f, [
            Column("shift", "Смена", 100),
            Column("records", "Записей", 70, "e", numeric=True),
            Column("availability", "Доступность", 96, "e", numeric=True, fmt=fmt_pct),
            Column("performance", "Производит.", 96, "e", numeric=True, fmt=fmt_pct),
            Column("quality", "Качество", 86, "e", numeric=True, fmt=fmt_pct),
            Column("oee", "OEE", 76, "e", numeric=True, fmt=fmt_pct),
            Column("total", "Произведено", 100, "e", numeric=True, fmt=fmt_int),
            Column("defects", "Брак", 80, "e", numeric=True, fmt=fmt_int),
        ], height=4, key="shift", empty_text="Нет данных")
        self.shift_table.pack(fill="both", expand=True)

    def _build_output(self):
        f = self.tabs["output"]
        self.output_chart = StackedBarChart(f, height=230, empty_text="Нет выпуска за период")
        self.output_chart.pack(fill="both", expand=True)
        tk.Label(f, text="Доля брака по дням", bg=C["surface"], fg=C["ink"], font=theme.FONTS.h3,
                 anchor="w").pack(fill="x", pady=(8, 0))
        self.defect_chart = LineChart(f, height=170, auto_scale=True, empty_text="Нет выпуска за период")
        self.defect_chart.pack(fill="both", expand=True)

    def _build_problems(self):
        f = self.tabs["problems"]
        top = tk.Frame(f, bg=C["surface"])
        top.pack(fill="x", pady=(0, 8))
        self.problem_label = tk.Label(top, text="", bg=C["surface"], fg=C["muted"], font=theme.FONTS.small)
        self.problem_label.pack(side="left")
        self.problems_table = DataTable(f, [
            Column("work_date", "Дата", 90, fmt=fmt_date),
            Column("shift", "Смена", 70),
            Column("equipment_name", "Оборудование", 200),
            Column("oee", "OEE", 70, "e", numeric=True, fmt=fmt_pct),
            Column("availability", "Доступн.", 76, "e", numeric=True, fmt=fmt_pct),
            Column("performance", "Произв-ть", 80, "e", numeric=True, fmt=fmt_pct),
            Column("quality", "Качество", 76, "e", numeric=True, fmt=fmt_pct),
            Column("issues", "Что не так", 360, stretch=True),
        ], height=14, empty_text="Проблемных смен за период нет")
        self.problems_table.pack(fill="both", expand=True)

    def _build_repeated(self):
        f = self.tabs["repeated"]
        tk.Label(f, text="Причины, повторявшиеся на одном оборудовании 2 и более раз за период.", bg=C["surface"],
                 fg=C["muted"], font=theme.FONTS.small, anchor="w").pack(fill="x", pady=(0, 8))
        self.repeated_table = DataTable(f, [
            Column("equipment_name", "Оборудование", 200, stretch=True),
            Column("reason", "Причина", 200),
            Column("count", "Повторов", 80, "e", numeric=True),
            Column("days", "Дней", 60, "e", numeric=True),
            Column("minutes", "Учтено, мин", 96, "e", numeric=True, fmt=fmt_min),
            Column("avg", "Средн., мин", 86, "e", numeric=True, fmt=fmt_min),
            Column("last", "Последний", 120, fmt=lambda v: formatting.ru_datetime(v)),
        ], height=14, key="key", empty_text="Повторяющихся проблем за период нет")
        self.repeated_table.pack(fill="both", expand=True)

    def _build_issues(self):
        f = self.tabs["issues"]
        tk.Label(f, text="Автоматические проверки: дубликаты смен, пересечения простоев, незавершённые простои, "
                         "выход коэффициентов за 100%, несоответствие простоев и времени работы.",
                 bg=C["surface"], fg=C["muted"], font=theme.FONTS.small, anchor="w", justify="left",
                 wraplength=900).pack(fill="x", pady=(0, 8))
        self.issues_table = DataTable(f, [
            Column("kind_label", "Тип", 150),
            Column("entity_label", "Запись", 130),
            Column("text", "Описание", 600, stretch=True),
        ], height=14, key="key", empty_text="Проверки пройдены: замечаний нет")
        self.issues_table.pack(fill="both", expand=True)

    def refresh(self):
        super().refresh()
        snap = self.app.snapshot()
        self._daily = snap.daily()
        down_by_day: dict = {}
        for d in snap.downtimes:
            down_by_day[d["work_date"]] = down_by_day.get(d["work_date"], 0.0) + d["counted_min"]
        self.trend_table.set_rows([{"day": day.isoformat(), "records": r.records, **result_fields(r),
                                    "total": r.total_count if r.has_data else None,
                                    "downtime": down_by_day.get(day.isoformat())}
                                   for day, r in self._daily if r.has_data or day.isoformat() in down_by_day])
        self._draw_trend()

        reasons = snap.reasons()
        self.reasons_chart.set_data([Bar(r["reason"], r["minutes"], f"{formatting.minutes(r['minutes'])} · "
                                                                    f"{fmt_pct(r['share'])}") for r in reasons])
        self.reasons_table.set_rows(reasons)

        equipment = snap.by_equipment()
        self.equipment_chart.set_data([Bar(e["equipment_name"], e["downtime_min"],
                                           formatting.minutes(e["downtime_min"]),
                                           f"{e['equipment_name']}\n{formatting.minutes(e['downtime_min'])}, "
                                           f"{e['downtime_count']} простоев, OEE {fmt_pct(e['result'].oee)}")
                                       for e in equipment])
        self.equipment_table.set_rows([{**e, **result_fields(e["result"]), "records": e["result"].records}
                                       for e in equipment])

        shifts = snap.by_shift()
        self.shift_chart.set_data([s for s, _ in shifts], [
            Series(name, METRIC_COLORS[key], [getattr(r, key) for _, r in shifts])
            for key, name in (("oee", "OEE"), ("availability", "Доступность"), ("performance", "Производительность"),
                              ("quality", "Качество"))])
        self.shift_table.set_rows([{"shift": s, "records": r.records, **result_fields(r), "total": r.total_count}
                                   for s, r in shifts])

        days = [d for d, _ in self._daily]
        good = [r.good_count if r.has_data else None for _, r in self._daily]
        defects = [r.defect_count if r.has_data else None for _, r in self._daily]

        def output_tip(i):
            r = self._daily[i][1]
            return (f"{formatting.ru_date(days[i])}\nГодные: {fmt_int(r.good_count)} шт.\n"
                    f"Брак: {fmt_int(r.defect_count)} шт. ({fmt_pct(1 - r.good_share if r.good_share is not None else None)})")
        self.output_chart.set_data(days, [Series("Годные", theme.GOOD_COLOR, good),
                                          Series("Брак", theme.DEFECT_COLOR, defects)], output_tip)
        self.defect_chart.set_data(days, [Series("Доля брака", theme.DEFECT_COLOR,
                                                 [(1 - r.good_share) if r.good_share is not None else None
                                                  for _, r in self._daily])])

        threshold = self.app.settings.oee_threshold_pct / 100
        self.problem_label.configure(text=f"Смены с OEE ниже {threshold * 100:.0f}% (порог меняется в настройках) "
                                          "или с предупреждениями расчёта, начиная с худших.")
        self.problems_table.set_rows([{**p["row"], **result_fields(p["result"]), "issues": "; ".join(p["issues"])}
                                      for p in snap.problem_records(threshold)])
        self.problems_table.set_tags(lambda r: ("warn",))
        self.repeated_table.set_rows([{**r, "key": f"{r['equipment_name']}|{r['reason']}"}
                                      for r in snap.repeated_problems()])
        kinds = {"duplicate": "Дубликат смены", "overlap": "Пересечение простоев", "open": "Незавершённый простой",
                 "calculation": "Предупреждение расчёта", "consistency": "Несогласованность"}
        self.issues_table.set_rows([{"key": i, "kind_label": kinds.get(issue.kind, issue.kind),
                                     "entity_label": ("Смена ID " if issue.entity == "production" else "Простой ID ")
                                     + str(issue.record_id), "text": issue.text}
                                    for i, issue in enumerate(snap.data_issues())])

    def export(self, kind: str):
        snap = self.app.snapshot()
        names = {"summary": "svodka", "production": "smeny", "downtimes": "prostoi"}
        period = snap.period
        suffix = f"{period[0]:%Y-%m-%d}_{period[1]:%Y-%m-%d}" if period else datetime.now().strftime("%Y-%m-%d")
        path = filedialog.asksaveasfilename(parent=self.app.root, title="Сохранить отчёт CSV", defaultextension=".csv",
                                            initialfile=f"pharmaoee_{names[kind]}_{suffix}.csv",
                                            filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        if snap.generated_count and not messagebox.askyesno(
                "Демонстрационные данные в отчёте",
                f"В выборку входят демонстрационные или симулированные записи ({snap.generated_count}). "
                "В отчёте будет предупреждение об этом.\n\nПродолжить экспорт?", parent=self.app.root):
            return
        description = self.app.filters_text()
        try:
            if kind == "summary":
                export.export_summary(path, snap, description)
                message = "Сводный отчёт сохранён."
            elif kind == "production":
                message = f"Экспортировано записей смен: {export.export_production(path, snap, description)}."
            else:
                message = f"Экспортировано простоев: {export.export_downtimes(path, snap, description)}."
        except DatabaseError as exc:
            messagebox.showerror("Экспорт не выполнен", describe_error(exc), parent=self.app.root)
            return
        self.app.notify(f"{message} Файл: {path}", "good")
