"""Сервисные разделы: демо-данные и симулятор, резервные копии, настройки и справка."""
from __future__ import annotations

import json
import platform
import sqlite3
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from .. import __version__, backup, demo, formatting, oee
from ..config import SHIFTS, SOURCE_DEMO, SOURCE_FILTERS, SOURCE_SIMULATOR
from ..db import DatabaseError, Filters, describe_error
from ..sources import (CONNECTION_LOST, RUNNING, STATE_LABELS, STOPPED, EventProcessor, ShiftSimulator,
                       TelemetryEvent)
from . import theme
from .charts import Timeline
from .theme import C
from .views import View, fmt_int, fmt_pct
from .widgets import (Banner, Card, Column, DataTable, DateEntry, IdCombobox, ScrollableFrame, Tooltip, form_label,
                      wrap_on_resize)

SPEEDS = {"Очень быстро (смена ≈ 10 с)": (20, 2), "Быстро (смена ≈ 25 с)": (50, 1),
          "Наглядно (1 мин смены = 0,2 с)": (200, 1), "Медленно (1 мин смены = 1 с)": (1000, 1)}
STATE_STYLE = {RUNNING: (C["good_soft"], C["good"]), STOPPED: (C["bad_soft"], C["bad"]),
               CONNECTION_LOST: (C["warn_soft"], C["warn"]), None: (C["surface_alt"], C["ink2"])}


def F():
    return theme.FONTS


def _row(parent, label: str, widget: tk.Widget, row: int, hint: str | None = None):
    form_label(parent, label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
    widget.grid(row=row, column=1, sticky="w", pady=5)
    if hint:
        tk.Label(parent, text=hint, bg=C["surface"], fg=C["muted"], font=F().caption).grid(
            row=row, column=2, sticky="w", padx=(10, 0))


# --- Демо-данные и симулятор ---------------------------------------------------------------

class DemoView(View):
    title = "Демо-данные и симулятор"
    subtitle = "Искусственные данные для обучения и демонстрации. Реальное оборудование не подключено."
    uses_filters = False

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.processor = EventProcessor(app.db)
        self.sim: ShiftSimulator | None = None
        self.sim_rows = {"event_ids": [], "downtime_ids": [], "production_ids": []}
        self._after = None
        self.paused = False
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        Banner(body, "Демонстрационный режим. Все записи этого раздела помечаются как «Демо-данные» или «Симулятор», "
                     "выделяются в таблицах и отчётах и удаляются отдельно, не затрагивая ручной ввод. "
                     "Подключения к PLC, SCADA, MES или датчикам в программе нет.", "warn").pack(
            fill="x", pady=(0, theme.GAP))
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, theme.GAP))
        top.columnconfigure(0, weight=3, uniform="t")
        top.columnconfigure(1, weight=2, uniform="t")
        self._build_generator(top)
        self._build_cleanup(top)
        self._build_simulator(body)
        self._build_signals(body)

    # Генератор ---------------------------------------------------------------------------
    def _build_generator(self, parent):
        card = Card(parent, "Генератор демонстрационных данных",
                    "Одинаковые настройки дают одинаковые данные. Существующие смены пропускаются.")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, theme.GAP / 2))
        b = card.body
        today = date.today()
        self.gen_from = DateEntry(b, on_pick=self._preview)
        self.gen_to = DateEntry(b, on_pick=self._preview)
        self.gen_from.set_date(today - timedelta(days=29))
        self.gen_to.set_date(today - timedelta(days=1))
        period = tk.Frame(b, bg=C["surface"])
        self.gen_from.pack(in_=period, side="left")
        tk.Label(period, text="—", bg=C["surface"], fg=C["muted"]).pack(side="left", padx=6)
        self.gen_to.pack(in_=period, side="left")
        self.gen_from.lift()
        self.gen_to.lift()
        _row(b, "Период (включительно)", period, 0, f"не более {demo.MAX_DAYS} дней")
        eq_frame = tk.Frame(b, bg=C["surface"])
        self.gen_equipment = {}
        for spec in demo.DEMO_EQUIPMENT:
            var = tk.BooleanVar(value=True)
            self.gen_equipment[spec.name] = var
            ttk.Checkbutton(eq_frame, text=spec.name.replace("ДЕМО — ", ""), variable=var,
                            style="Card.TCheckbutton", command=self._preview).pack(side="left", padx=(0, 10))
        _row(b, "Демо-линии", eq_frame, 1)
        shift_frame = tk.Frame(b, bg=C["surface"])
        self.gen_shifts = {}
        for i, shift in enumerate(SHIFTS):
            var = tk.BooleanVar(value=i == 0)
            self.gen_shifts[shift] = var
            ttk.Checkbutton(shift_frame, text=shift, variable=var, style="Card.TCheckbutton",
                            command=self._preview).pack(side="left", padx=(0, 10))
        _row(b, "Смены", shift_frame, 2, "3 смена переходит через полночь")
        self.gen_seed = tk.StringVar(value=str(demo.DEFAULT_SEED))
        seed = ttk.Entry(b, textvariable=self.gen_seed, width=10)
        _row(b, "Зерно генератора", seed, 3, "другое зерно — другой набор данных")
        self.gen_preview = tk.Label(b, text="", bg=C["surface"], fg=C["ink2"], font=F().small, anchor="w",
                                    justify="left")
        self.gen_preview.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        b.columnconfigure(2, weight=1)
        wrap_on_resize(self.gen_preview)
        self.gen_button = ttk.Button(b, text="Создать демо-данные", style="Primary.TButton", command=self.generate)
        self.gen_button.grid(row=5, column=0, sticky="w", pady=(4, 0))
        for var in (self.gen_from.var, self.gen_to.var, self.gen_seed):
            var.trace_add("write", lambda *_: self._preview())
        self._preview()

    def _request(self) -> demo.DemoRequest:
        start, end = self.gen_from.get_date(), self.gen_to.get_date()
        if start is None or end is None:
            raise ValueError("Введите обе даты периода в формате ДД.ММ.ГГГГ.")
        try:
            seed = int(self.gen_seed.get().strip())
        except ValueError:
            raise ValueError("Зерно генератора — целое число.") from None
        request = demo.DemoRequest(start, end, tuple(n for n, v in self.gen_equipment.items() if v.get()),
                                   tuple(s for s, v in self.gen_shifts.items() if v.get()), seed)
        request.validate(date.today())
        return request

    def _preview(self):
        try:
            request = self._request()
        except ValueError as exc:
            self.gen_preview.configure(text=str(exc), fg=C["bad"])
            return
        days = (request.end - request.start).days + 1
        self.gen_preview.configure(fg=C["ink2"], text=(
            f"Будет обработано {request.planned_shifts} смен: {days} дн. × {len(request.equipment)} лин. × "
            f"{len(request.shifts)} см. Уже существующие и ещё не завершённые смены будут пропущены."))

    def generate(self):
        try:
            request = self._request()
        except ValueError as exc:
            messagebox.showerror("Демо-данные", str(exc), parent=self.app.root)
            return
        if not messagebox.askyesno("Демо-данные", f"Создать демонстрационные данные с "
                                   f"{formatting.ru_date(request.start)} по {formatting.ru_date(request.end)}?\n\n"
                                   "Это ИСКУССТВЕННЫЕ данные для проверки приложения, не результаты производства.",
                                   parent=self.app.root):
            return
        self.gen_button.state(["disabled"])

        def done(result: demo.DemoResult):
            self.gen_button.state(["!disabled"])
            text = (f"Создано смен: {result.created_shifts}, простоев: {result.created_downtimes}. "
                    f"Пропущено (уже есть): {result.skipped}.")
            if result.skipped_unfinished:
                text += f" Пропущено незавершённых смен: {result.skipped_unfinished}."
            self.app.set_period(result.start, result.end)
            self.app.data_changed(text)
            messagebox.showinfo("Демо-данные", text + "\n\nПериод фильтра установлен на "
                                f"{formatting.ru_date(result.start)} — {formatting.ru_date(result.end)}.",
                                parent=self.app.root)

        def failed(exc):
            self.gen_button.state(["!disabled"])
            messagebox.showerror("Демо-данные не созданы", describe_error(exc), parent=self.app.root)

        self.app.run_background(lambda: demo.generate(self.app.db, request), done, failed,
                                "Создание демонстрационных данных…")

    # Очистка --------------------------------------------------------------------------------
    def _build_cleanup(self, parent):
        card = Card(parent, "Удаление сгенерированных данных", "Ручной ввод не удаляется никогда.")
        card.grid(row=0, column=1, sticky="nsew", padx=(theme.GAP / 2, 0))
        self.counts_label = tk.Label(card.body, text="", bg=C["surface"], fg=C["ink"], font=F().body, anchor="w",
                                     justify="left")
        self.counts_label.pack(fill="x", pady=(0, 10))
        ttk.Button(card.body, text="Удалить демо-данные…", style="Danger.TButton",
                   command=lambda: self.cleanup(SOURCE_DEMO)).pack(anchor="w")
        ttk.Button(card.body, text="Удалить данные симулятора…", style="Danger.TButton",
                   command=lambda: self.cleanup(SOURCE_SIMULATOR)).pack(anchor="w", pady=(6, 0))
        tk.Label(card.body, text="Перед удалением автоматически создаётся резервная копия базы.", bg=C["surface"],
                 fg=C["muted"], font=F().caption, anchor="w", justify="left").pack(fill="x", pady=(10, 0))

    def cleanup(self, source: str):
        counts = self.app.db.source_counts().get(source, {})
        name = "демонстрационные данные" if source == SOURCE_DEMO else "данные симулятора"
        if not any(counts.values()):
            messagebox.showinfo("Удаление", f"В базе нет записей: {name}.", parent=self.app.root)
            return
        if self.sim is not None and source == SOURCE_SIMULATOR:
            messagebox.showwarning("Удаление", "Сначала остановите симулятор.", parent=self.app.root)
            return
        text = (f"Удалить {name}?\n\nСмен: {counts.get('production', 0)}, простоев: {counts.get('downtimes', 0)}, "
                f"событий: {counts.get('sensor_events', 0)}.\n\nЗаписи с источником «Ручной ввод» не затрагиваются. "
                "Перед удалением будет создана резервная копия.")
        if not messagebox.askyesno("Удаление", text, icon="warning", default="no", parent=self.app.root):
            return

        def work():
            backup.create_backup(self.app.db.path, self.app.paths.backups_dir / backup.timestamped_name(f"before_delete_{source}"))
            return self.app.db.delete_generated(source)

        def done(result):
            self.app.data_changed(f"Удалено: смен {result['production']}, простоев {result['downtimes']}, "
                                  f"событий {result['sensor_events']}.")

        self.app.run_background(work, done, lambda e: messagebox.showerror("Удаление", describe_error(e),
                                                                            parent=self.app.root), "Удаление…")

    # Симулятор ------------------------------------------------------------------------------
    def _build_simulator(self, parent):
        card = Card(parent, "Симулятор смены", "Модель работы демо-линии: остановки, потеря связи, выпуск и брак. "
                                               "По окончании сеанса сохраняется запись смены с источником «Симулятор».")
        card.pack(fill="x", pady=(0, theme.GAP))
        b = card.body
        form = tk.Frame(b, bg=C["surface"])
        form.pack(fill="x")
        self.sim_picker = IdCombobox(form, width=30)
        _row(form, "Демо-линия", self.sim_picker, 0)
        self.prepare_button = ttk.Button(form, text="Подготовить демо-линии", command=self.prepare_equipment)
        self.sim_date = DateEntry(form)
        self.sim_date.set_date(date.today() - timedelta(days=1))
        _row(form, "Дата смены", self.sim_date, 1)
        self.sim_shift = tk.StringVar(value=SHIFTS[0])
        _row(form, "Смена", ttk.Combobox(form, textvariable=self.sim_shift, values=SHIFTS, state="readonly",
                                         width=12), 2)
        self.sim_speed = tk.StringVar(value=list(SPEEDS)[1])
        _row(form, "Скорость", ttk.Combobox(form, textvariable=self.sim_speed, values=list(SPEEDS),
                                            state="readonly", width=32), 3)
        buttons = tk.Frame(b, bg=C["surface"])
        buttons.pack(fill="x", pady=(10, 10))
        self.start_button = ttk.Button(buttons, text="Запустить", style="Primary.TButton", command=self.start_sim)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(buttons, text="Пауза", command=self.pause_sim)
        self.pause_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(buttons, text="Прервать и отменить", style="Danger.TButton",
                                      command=self.abort_sim)
        self.stop_button.pack(side="left")
        Tooltip(self.stop_button, "Удаляет все записи текущего сеанса симулятора")
        self.sim_state = tk.Label(b, text="Симулятор не запущен", font=F().h2, anchor="w", padx=14, pady=8)
        self.sim_state.pack(fill="x")
        self.sim_progress = ttk.Progressbar(b, mode="determinate", maximum=480)
        self.sim_progress.pack(fill="x", pady=(8, 0))
        stats = tk.Frame(b, bg=C["surface"])
        stats.pack(fill="x", pady=(8, 0))
        self.sim_values = {}
        for i, (key, name) in enumerate((("total", "Произведено, шт."), ("good", "Годные, шт."),
                                         ("defects", "Брак, шт."), ("run", "Работа, мин"), ("stop", "Простои, мин"),
                                         ("lost", "Нет связи, мин"), ("oee", "OEE (текущий)"))):
            cell = tk.Frame(stats, bg=C["surface"])
            cell.grid(row=0, column=i, sticky="w", padx=(0, 22))
            tk.Label(cell, text=name, bg=C["surface"], fg=C["muted"], font=F().caption).pack(anchor="w")
            value = tk.Label(cell, text="—", bg=C["surface"], fg=C["ink"], font=F().h2)
            value.pack(anchor="w")
            self.sim_values[key] = value
        tk.Label(b, text="Во время потери связи счётчики выпуска не передаются и обновятся после восстановления. "
                         "Потеря связи не считается простоем: состояние оборудования в этот период неизвестно.",
                 bg=C["surface"], fg=C["muted"], font=F().caption, anchor="w", justify="left",
                 wraplength=900).pack(fill="x", pady=(6, 0))
        self.timeline = Timeline(b, height=92)
        self.timeline.pack(fill="x", pady=(8, 0))
        self._sim_buttons()

    def prepare_equipment(self):
        if self.guard(lambda: demo.ensure_demo_equipment(self.app.db)) is not None:
            self.app.data_changed("Демо-линии добавлены в справочник оборудования.")

    def _sim_buttons(self):
        running = self.sim is not None
        self.start_button.state(["disabled"] if running else ["!disabled"])
        self.pause_button.state(["!disabled"] if running else ["disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        self.pause_button.configure(text="Продолжить" if self.paused else "Пауза")

    def start_sim(self):
        eq_id, day = self.sim_picker.get_id(), self.sim_date.get_date()
        if eq_id is None:
            messagebox.showinfo("Симулятор", "Выберите демо-линию. Если список пуст, нажмите «Подготовить демо-линии».",
                                parent=self.app.root)
            return
        if day is None or day > date.today():
            messagebox.showerror("Симулятор", "Укажите прошедшую или сегодняшнюю дату смены (ДД.ММ.ГГГГ).",
                                 parent=self.app.root)
            return
        shift = self.sim_shift.get()
        if self.app.db.count_production(Filters(day, day, eq_id, shift)):
            messagebox.showerror("Симулятор", "Для этой линии, даты и смены уже есть запись. Выберите другую "
                                              "дату или смену, чтобы не создавать дубликат.", parent=self.app.root)
            return
        equipment = self.app.db.get_equipment(eq_id)
        self.sim = ShiftSimulator(eq_id, equipment["name"], day, shift, equipment["ideal_cycle_sec"] or 1.0,
                                  seed=day.toordinal())
        self.sim_rows = {"event_ids": [], "downtime_ids": [], "production_ids": []}
        self.paused = False
        self.sim_progress.configure(maximum=self.sim.length, value=0)
        self._sim_buttons()
        self.app.notify("Симулятор запущен (демонстрационные данные).", "info")
        self._tick()

    def _tick(self):
        self._after = None
        if self.sim is None or self.paused:
            return
        interval, steps = SPEEDS.get(self.sim_speed.get(), (50, 1))
        try:
            for _ in range(steps):
                for event in self.sim.step():
                    result = self.processor.apply(event)
                    if result.event_id:
                        self.sim_rows["event_ids"].append(result.event_id)
                    if result.downtime_id:
                        self.sim_rows["downtime_ids"].append(result.downtime_id)
                if self.sim.finished:
                    break
        except DatabaseError as exc:
            self._stop_after_error(exc)
            return
        except sqlite3.Error as exc:
            self._stop_after_error(exc)
            return
        self._show_sim()
        if self.sim.finished:
            self._finish_sim()
        else:
            self._after = self.after(interval, self._tick)

    def _stop_after_error(self, exc):
        self.app.log_exception(exc)
        messagebox.showerror("Симулятор остановлен", describe_error(exc) + "\nЗаписи сеанса будут отменены.",
                             parent=self.app.root)
        self._rollback()

    def _show_sim(self):
        sim = self.sim
        c = sim.counters
        state = sim.reported
        bg, fg = STATE_STYLE.get(state, STATE_STYLE[None])
        clock = sim.clock.strftime("%d.%m %H:%M")
        self.sim_state.configure(text=f"{STATE_LABELS.get(state, '—')} · время смены {clock} · "
                                      f"{c.minute} из {sim.length} мин", bg=bg, fg=fg)
        self.sim_progress.configure(value=c.minute)
        res = oee.calculate(max(c.minute, 1), c.run_min, c.reported_total, c.reported_good, sim.ideal_cycle_sec)
        self.sim_values["total"].configure(text=fmt_int(c.reported_total))
        self.sim_values["good"].configure(text=fmt_int(c.reported_good))
        self.sim_values["defects"].configure(text=fmt_int(c.reported_total - c.reported_good))
        self.sim_values["run"].configure(text=str(c.run_min))
        self.sim_values["stop"].configure(text=str(c.stop_min))
        self.sim_values["lost"].configure(text=str(c.lost_min))
        self.sim_values["oee"].configure(text=fmt_pct(res.oee))
        segments = []
        timeline = sim.timeline
        for i, (minute, st) in enumerate(timeline):
            end = timeline[i + 1][0] if i + 1 < len(timeline) else c.minute
            if end > minute:
                segments.append((minute, end, st))
        self.timeline.set_data(segments, sim.length,
                               lambda m, s=sim.start: (s + timedelta(minutes=m)).strftime("%H:%M"))

    def _finish_sim(self):
        sim = self.sim
        try:
            self.processor.close_session(sim.equipment_id, sim.end)
            record_id = self.app.db.add_production(sim.production_record(), source=SOURCE_SIMULATOR)
        except DatabaseError as exc:
            self._stop_after_error(exc)
            return
        c = sim.counters
        self.sim = None
        self._sim_buttons()
        self.app.set_period(sim.work_date, sim.work_date)
        self.app.data_changed(f"Сеанс симулятора завершён: запись смены ID {record_id}, выпуск {fmt_int(c.total)} шт., "
                              f"остановок {c.stops}.")

    def pause_sim(self):
        if self.sim is None:
            return
        self.paused = not self.paused
        self._sim_buttons()
        if not self.paused:
            self._tick()
        elif self._after:
            self.after_cancel(self._after)
            self._after = None

    def abort_sim(self, ask: bool = True) -> bool:
        if self.sim is None:
            return True
        if ask and not messagebox.askyesno("Прервать симулятор", "Прервать сеанс? Все записи этого сеанса "
                                                                "(события и простои) будут удалены.",
                                           parent=self.app.root):
            return False
        self._rollback()
        return True

    def _rollback(self):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        sim = self.sim
        self.sim = None
        self.paused = False
        try:
            self.app.db.delete_rows(**self.sim_rows)
        except DatabaseError as exc:
            messagebox.showerror("Симулятор", describe_error(exc), parent=self.app.root)
        self.sim_state.configure(text="Сеанс прерван, записи сеанса удалены", **dict(zip(("bg", "fg"), STATE_STYLE[None])))
        self.timeline.set_data([], sim.length if sim else 480, str)
        self._sim_buttons()
        self.app.data_changed("Сеанс симулятора отменён.")

    def destroy(self):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        super().destroy()

    @property
    def simulation_running(self) -> bool:
        return self.sim is not None

    # Ручные сигналы --------------------------------------------------------------------------
    def _build_signals(self, parent):
        card = Card(parent, "Ручные сигналы оборудования (имитация датчика)",
                    "Кнопки имитируют сигнал датчика текущим временем. «Остановка» открывает простой с причиной "
                    "«Не классифицировано» — причину указывает оператор в разделе «Простои».")
        card.pack(fill="both", expand=True)
        b = card.body
        form = tk.Frame(b, bg=C["surface"])
        form.pack(fill="x")
        self.sig_picker = IdCombobox(form, width=30)
        self.sig_picker.bind("<<ComboboxSelected>>", lambda e: self._show_signal_state())
        _row(form, "Оборудование", self.sig_picker, 0)
        self.sig_state = tk.Label(b, text="", font=F().h3, anchor="w", padx=14, pady=8)
        self.sig_state.pack(fill="x", pady=(8, 8))
        buttons = tk.Frame(b, bg=C["surface"])
        buttons.pack(fill="x", pady=(0, 10))
        for state, text in ((RUNNING, "Сигнал: РАБОТАЕТ"), (STOPPED, "Сигнал: ОСТАНОВКА"),
                            (CONNECTION_LOST, "Сигнал: ПОТЕРЯ СВЯЗИ")):
            ttk.Button(buttons, text=text, style="Primary.TButton" if state == RUNNING else "TButton",
                       command=lambda s=state: self.signal(s)).pack(side="left", padx=(0, 6))
        self.events = DataTable(b, [
            Column("start_at", "Начало", 130, fmt=lambda v: formatting.ru_datetime(v, True)),
            Column("end_at", "Окончание", 130, fmt=lambda v: formatting.ru_datetime(v, True) if v else "текущее"),
            Column("equipment_name", "Оборудование", 180),
            Column("state_label", "Состояние", 100),
            Column("source_name", "Источник", 170),
            Column("downtime", "Простой", 140),
            Column("comment", "Комментарий", 260, stretch=True),
        ], height=8, empty_text="Событий пока нет")
        self.events.pack(fill="both", expand=True)

    def _show_signal_state(self):
        eq_id = self.sig_picker.get_id()
        current = self.app.db.current_state(eq_id) if eq_id else None
        state = current["state"] if current else None
        bg, fg = STATE_STYLE.get(state, STATE_STYLE[None])
        if current:
            text = (f"Текущее состояние: {STATE_LABELS[state]} с {formatting.ru_datetime(current['start_at'], True)} "
                    f"(источник: {current['source_name']})")
        else:
            text = "Состояние неизвестно: сигналов по этому оборудованию ещё не было"
        self.sig_state.configure(text=text, bg=bg, fg=fg)

    def signal(self, state: str):
        eq_id = self.sig_picker.get_id()
        if eq_id is None:
            messagebox.showinfo("Сигнал", "Выберите оборудование.", parent=self.app.root)
            return
        now = datetime.now().replace(microsecond=0)
        event = TelemetryEvent(eq_id, state, now, "Ручные сигналы (имитация)", now)
        result = self.guard(lambda: self.processor.apply(event))
        if result is None:
            return
        kind = {"applied": "good", "ignored": "warn", "rejected": "bad"}[result.status]
        self.app.data_changed(result.message, kind)

    def refresh(self):
        super().refresh()
        equipment = self.app.db.list_equipment()
        demo_items = [(e["id"], e["name"]) for e in equipment if e["is_demo"] and e["is_active"]]
        self.sim_picker.set_items(demo_items)
        if demo_items:
            self.prepare_button.grid_forget()
        else:
            self.prepare_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.sig_picker.set_items([(e["id"], e["name"]) for e in equipment if e["is_active"]])
        counts = self.app.db.source_counts()
        d, s = counts.get(SOURCE_DEMO, {}), counts.get(SOURCE_SIMULATOR, {})
        self.counts_label.configure(text=(
            f"Демо-данные: смен {d.get('production', 0)}, простоев {d.get('downtimes', 0)}\n"
            f"Симулятор: смен {s.get('production', 0)}, простоев {s.get('downtimes', 0)}, "
            f"событий {s.get('sensor_events', 0)}"))
        rows = []
        for e in self.app.db.list_events(200):
            rows.append({**e, "state_label": STATE_LABELS.get(e["state"], e["state"]),
                         "downtime": (f"ID {e['downtime_id']} · {e['downtime_reason']}" if e["downtime_id"] else "")})
        self.events.set_rows(rows)
        self._show_signal_state()
        if self.sim is None:
            self.sim_state.configure(text="Симулятор не запущен", bg=STATE_STYLE[None][0], fg=STATE_STYLE[None][1])


# --- Резервное копирование ------------------------------------------------------------------------

class BackupView(View):
    title = "Резервное копирование"
    subtitle = "Копии базы создаются через backup API SQLite; восстановление проверяет файл и сохраняет текущую базу."
    uses_filters = False

    def __init__(self, parent, app):
        super().__init__(parent, app)
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        info = Card(body, "Текущая база данных")
        info.pack(fill="x", pady=(0, theme.GAP))
        grid = tk.Frame(info.body, bg=C["surface"])
        grid.pack(fill="x")
        self.path_var = tk.StringVar(value=str(app.db.path))
        path_entry = ttk.Entry(grid, textvariable=self.path_var, state="readonly", width=80)
        _row(grid, "Файл базы", path_entry, 0)
        self.info_label = tk.Label(grid, text="", bg=C["surface"], fg=C["ink"], font=F().body, justify="left",
                                   anchor="w")
        _row(grid, "Состояние", self.info_label, 1)
        actions = tk.Frame(info.body, bg=C["surface"])
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Открыть папку данных", command=lambda: app.open_path(app.paths.data_dir)).pack(side="left")
        ttk.Button(actions, text="Проверить целостность", command=self.check).pack(side="left", padx=6)

        make = Card(body, "Создать резервную копию",
                    "Рекомендуется перед крупными изменениями, удалением данных и обновлением программы.")
        make.pack(fill="x", pady=(0, theme.GAP))
        row = tk.Frame(make.body, bg=C["surface"])
        row.pack(fill="x")
        ttk.Button(row, text="Создать копию в папке резервных копий", style="Primary.TButton",
                   command=self.quick_backup).pack(side="left")
        ttk.Button(row, text="Сохранить копию как…", command=self.backup_as).pack(side="left", padx=6)

        restore = Card(body, "Восстановление из резервной копии",
                       "1) файл проверяется (формат, целостность, версия); 2) создаётся копия текущей базы; "
                       "3) данные готовятся во временном файле; 4) содержимое заменяется одной операцией. "
                       "При ошибке текущая база остаётся без изменений. Можно восстановить и базу PharmaOEE v8 — "
                       "она будет обновлена до новой схемы.")
        restore.pack(fill="x", pady=(0, theme.GAP))
        ttk.Button(restore.body, text="Восстановить из файла…", command=self.restore_from_file).pack(anchor="w")

        listing = Card(body, "Резервные копии в папке", str(app.paths.backups_dir))
        listing.pack(fill="both", expand=True)
        ttk.Button(listing.actions, text="Открыть папку",
                   command=lambda: app.open_path(app.paths.backups_dir)).pack(side="left")
        self.table = DataTable(listing.body, [
            Column("name", "Файл", 380, stretch=True),
            Column("modified", "Изменён", 140, fmt=lambda v: formatting.ru_datetime(v)),
            Column("size", "Размер, КБ", 90, "e", numeric=True, fmt=lambda v: formatting.integer(v / 1024)),
        ], height=8, key="name", empty_text="Резервных копий в папке пока нет")
        self.table.pack(fill="both", expand=True)
        buttons = tk.Frame(listing.body, bg=C["surface"])
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Проверить выбранную", command=self.inspect_selected).pack(side="left")
        ttk.Button(buttons, text="Восстановить выбранную…", command=self.restore_selected).pack(side="left", padx=6)

    def refresh(self):
        super().refresh()
        try:
            stats = self.app.db.stats()
        except DatabaseError as exc:
            self.info_label.configure(text=describe_error(exc), fg=C["bad"])
            return
        except sqlite3.Error as exc:
            self.info_label.configure(text=describe_error(exc), fg=C["bad"])
            return
        self.info_label.configure(fg=C["ink"], text=(
            f"Размер: {formatting.integer(stats['size_bytes'] / 1024)} КБ · версия схемы: {stats['schema_version']}\n"
            f"Оборудование: {stats['equipment']} · записей смен: {stats['production']} · простоев: "
            f"{stats['downtimes']} · событий: {stats['sensor_events']} · изменений в журнале: {stats['change_log']}"))
        rows = []
        for path in backup.list_backups(self.app.paths.backups_dir):
            st = path.stat()
            rows.append({"name": path.name, "path": path, "modified": datetime.fromtimestamp(st.st_mtime),
                         "size": st.st_size})
        self.table.set_rows(rows)

    def check(self):
        def done(result):
            if result == "ok":
                messagebox.showinfo("Проверка целостности", "База данных в порядке (integrity_check: ok).",
                                    parent=self.app.root)
            else:
                messagebox.showerror("Проверка целостности", f"Обнаружены проблемы: {result}\n\nРекомендуется "
                                     "восстановить базу из резервной копии.", parent=self.app.root)
        self.app.run_background(self.app.db.integrity_check, done, self._error, "Проверка целостности…")

    def _error(self, exc):
        messagebox.showerror("Резервное копирование", describe_error(exc), parent=self.app.root)

    def _backup_to(self, target):
        def done(path):
            self.refresh()
            self.app.notify(f"Резервная копия создана: {path}", "good")
            messagebox.showinfo("Резервная копия", f"Копия базы создана и проверена:\n{path}", parent=self.app.root)
        self.app.run_background(lambda: backup.create_backup(self.app.db.path, target), done, self._error,
                                "Создание резервной копии…")

    def quick_backup(self):
        self._backup_to(self.app.paths.backups_dir / backup.timestamped_name("manual"))

    def backup_as(self):
        target = filedialog.asksaveasfilename(parent=self.app.root, title="Сохранить резервную копию",
                                              defaultextension=".db",
                                              initialfile=backup.timestamped_name("backup"),
                                              filetypes=[("База SQLite", "*.db"), ("Все файлы", "*.*")])
        if target:
            self._backup_to(target)

    def _selected_path(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Резервные копии", "Выберите файл в списке.", parent=self.app.root)
        return row["path"] if row else None

    def inspect_selected(self):
        path = self._selected_path()
        if not path:
            return
        try:
            info = backup.inspect_backup(path)
        except DatabaseError as exc:
            self._error(exc)
            return
        counts = ", ".join(f"{k}: {v}" for k, v in info.counts.items())
        messagebox.showinfo("Проверка копии", f"{path.name}\n\nФормат: {info.kind_label}\nЦелостность: ok\n"
                            f"Записей — {counts}", parent=self.app.root)

    def restore_selected(self):
        path = self._selected_path()
        if path:
            self.restore(path)

    def restore_from_file(self):
        path = filedialog.askopenfilename(parent=self.app.root, title="Выберите резервную копию",
                                          initialdir=str(self.app.paths.backups_dir),
                                          filetypes=[("База SQLite", "*.db"), ("Все файлы", "*.*")])
        if path:
            from pathlib import Path
            self.restore(Path(path))

    def restore(self, path):
        if self.app.simulation_running():
            messagebox.showwarning("Восстановление", "Сначала остановите симулятор.", parent=self.app.root)
            return
        try:
            info = backup.inspect_backup(path)
        except DatabaseError as exc:
            messagebox.showerror("Файл не подходит", describe_error(exc), parent=self.app.root)
            return
        counts = "\n".join(f"  {k}: {v}" for k, v in info.counts.items())
        if not messagebox.askyesno(
                "Восстановление базы",
                f"Заменить текущие данные содержимым копии?\n\nФайл: {path}\nФормат: {info.kind_label}\n"
                f"Изменён: {formatting.ru_datetime(info.modified)}\nЗаписи:\n{counts}\n\n"
                f"Перед заменой текущая база будет сохранена в папку:\n{self.app.paths.backups_dir}",
                icon="warning", default="no", parent=self.app.root):
            return

        def done(result):
            self.app.data_changed("База восстановлена из резервной копии.")
            messagebox.showinfo("Восстановление выполнено", f"Данные восстановлены из:\n{result.restored_from}\n\n"
                                f"Копия прежней базы:\n{result.safety_backup}", parent=self.app.root)

        self.app.run_background(lambda: backup.restore_backup(self.app.db, path, self.app.paths.backups_dir),
                                done, self._error, "Восстановление базы…")


# --- Настройки и справка ------------------------------------------------------------------------------

FORMULAS = """Доступность (Availability) = время работы / плановое время
Производительность (Performance) = произведено × идеальный цикл / время работы
Качество (Quality) = годные / произведено
OEE = Доступность × Производительность × Качество = годные × идеальный цикл / плановое время

Правила расчёта:
• Значения не обрезаются до 100%. Производительность выше 100% показывается с предупреждением —
  проверьте идеальный цикл, состав продукции и исходные данные.
• При сохранении блокируются: время работы больше планового, годных больше произведённых,
  нулевое плановое время, нулевой или отрицательный цикл, выпуск при нулевом времени работы.
• Для нескольких записей суммируются минуты и идеальное время выпуска (Σ штук × цикл).
  Качество взвешивается идеальным временем, поэтому A × P × Q = OEE и для записей с разным циклом.
• Период фильтруется по дате смены, обе границы включаются. Ночная смена относится к дате своего начала.
• Время OEE берётся из записи смены. Журнал простоев — отдельный учёт: кнопка «Из простоев» в форме
  смены считает время работы как «план − простои», а проверка данных сообщает о расхождениях.
• Простои одного оборудования не могут пересекаться. Если пересечения есть в старых данных,
  время считается по объединению интервалов (без двойного учёта), а общая часть относится к простою,
  начавшемуся раньше.
• Время хранится как местное время компьютера без часового пояса. При переходе на летнее время
  длительность интервала, пересекающего переход, может отличаться на 1 час."""

LIMITS = """PharmaOEE — учебный прототип для демонстрации и обучения.
• Не является промышленной или GMP-валидированной системой; соответствие 21 CFR Part 11 не заявляется.
• Нет разграничения прав, электронных подписей и защищённого от изменения аудиторского следа.
  Журнал изменений ниже — обычная таблица базы данных, её можно изменить средствами SQLite.
• Подключения к реальному оборудованию (PLC, SCADA, MES, OPC UA) нет; источники событий — только симулятор.
• Не вводите коммерческую тайну и реальные производственные данные без разрешения владельца."""


class SettingsView(View):
    title = "Настройки и справка"
    subtitle = "Параметры отображения, формулы, ограничения и журнал изменений."
    uses_filters = False

    def __init__(self, parent, app):
        super().__init__(parent, app)
        scroll = ScrollableFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        settings = Card(body, "Настройки")
        settings.pack(fill="x", pady=(0, theme.GAP))
        grid = tk.Frame(settings.body, bg=C["surface"])
        grid.pack(fill="x")
        self.threshold = tk.StringVar(value=f"{app.settings.oee_threshold_pct:g}")
        _row(grid, "Порог OEE, %", ttk.Spinbox(grid, from_=1, to=99, textvariable=self.threshold, width=6), 0,
             "ниже порога — подсветка и список «Проблемные смены»; это ориентир, а не норматив")
        self.default_source = tk.StringVar(value=SOURCE_FILTERS[app.settings.default_source_filter])
        _row(grid, "Данные по умолчанию", ttk.Combobox(grid, textvariable=self.default_source, state="readonly",
                                                       values=list(SOURCE_FILTERS.values()), width=26), 1,
             "какие записи учитывать при запуске программы")
        ttk.Button(settings.body, text="Сохранить настройки", style="Primary.TButton", command=self.save).pack(
            anchor="w", pady=(10, 0))

        formulas = Card(body, "Формулы и правила расчёта")
        formulas.pack(fill="x", pady=(0, theme.GAP))
        tk.Label(formulas.body, text=FORMULAS, bg=C["surface"], fg=C["ink"], font=F().body, justify="left",
                 anchor="w").pack(fill="x")

        about = Card(body, f"О программе — PharmaOEE {__version__}")
        about.pack(fill="x", pady=(0, theme.GAP))
        tk.Label(about.body, text=LIMITS, bg=C["surface"], fg=C["ink"], font=F().body, justify="left",
                 anchor="w").pack(fill="x")
        tk.Label(about.body, text=(
            f"\nPython {platform.python_version()} · Tk {app.root.tk.call('info', 'patchlevel')} · "
            f"SQLite {sqlite3.sqlite_version} · {platform.system()} {platform.release()}\n"
            f"Данные: {app.paths.data_dir}\nЖурнал работы: {app.paths.log_path}"),
            bg=C["surface"], fg=C["muted"], font=F().small, justify="left", anchor="w").pack(fill="x")
        buttons = tk.Frame(about.body, bg=C["surface"])
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Открыть папку данных", command=lambda: app.open_path(app.paths.data_dir)).pack(side="left")
        ttk.Button(buttons, text="Открыть журнал работы", command=lambda: app.open_path(app.paths.log_path)).pack(
            side="left", padx=6)

        changes = Card(body, "Журнал изменений", "Последние 300 операций создания, изменения и удаления записей.")
        changes.pack(fill="both", expand=True)
        self.changes = DataTable(changes.body, [
            Column("changed_at", "Время", 130, fmt=lambda v: formatting.ru_datetime(v, True)),
            Column("action_label", "Действие", 100),
            Column("entity_label", "Объект", 130),
            Column("entity_id", "ID", 60, "e", numeric=True),
            Column("details_text", "Подробности", 520, stretch=True),
        ], height=10, empty_text="Изменений пока нет")
        self.changes.pack(fill="both", expand=True)

    ACTIONS = {"create": "создание", "update": "изменение", "delete": "удаление", "close": "завершение",
               "migrate": "миграция"}
    ENTITIES = {"production": "запись смены", "downtime": "простой", "equipment": "оборудование",
                "reason": "причина", "database": "база данных", "demo": "демо-данные", "simulator": "симулятор"}

    def refresh(self):
        super().refresh()
        rows = []
        for r in self.app.db.list_changes(300):
            details = r["details"]
            try:
                parsed = json.loads(details)
                if isinstance(parsed, dict):
                    details = "; ".join(f"{k}: {v}" for k, v in parsed.items())
            except ValueError:
                pass
            rows.append({**r, "action_label": self.ACTIONS.get(r["action"], r["action"]),
                         "entity_label": self.ENTITIES.get(r["entity"], r["entity"]),
                         "details_text": details[:400]})
        self.changes.set_rows(rows)

    def save(self):
        try:
            value = float(self.threshold.get().replace(",", "."))
            if not 0 < value < 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("Настройки", "Порог OEE — число от 1 до 99.", parent=self.app.root)
            return
        self.app.settings.oee_threshold_pct = value
        self.app.settings.default_source_filter = next(k for k, v in SOURCE_FILTERS.items()
                                                       if v == self.default_source.get())
        try:
            self.app.settings.save(self.app.paths.settings_path)
        except OSError as exc:
            messagebox.showerror("Настройки", describe_error(exc), parent=self.app.root)
            return
        self.app.data_changed("Настройки сохранены.")
