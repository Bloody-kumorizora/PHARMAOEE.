"""Модальные формы ввода: смена, простой, оборудование, справочник причин.

Изменения записываются только по кнопке «Сохранить»; «Отмена» и Esc закрывают
форму без сохранения (с вопросом, если что-то уже введено). Кнопка сохранения
блокируется на время записи — повторное нажатие не создаст дубликат.
"""
from __future__ import annotations

import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox, simpledialog, ttk

from .. import formatting, oee, validation
from ..config import SHIFT_SCHEDULE, SHIFTS, SOURCE_LABELS, SOURCE_MANUAL, UNCLASSIFIED_REASON
from ..db import DatabaseError, describe_error, parse_ts
from . import theme
from .theme import C
from .widgets import Column, DataTable, DateEntry, IdCombobox, Tooltip, form_label


def F():
    return theme.FONTS


class FormDialog(tk.Toplevel):
    def __init__(self, app, title: str, subtitle: str = ""):
        super().__init__(app.root)
        self.app = app
        self.withdraw()
        self.title(title)
        self.configure(bg=C["surface"])
        self.transient(app.root)
        self.resizable(True, False)
        self.result = None
        self._saving = False
        self._initial: dict | None = None
        head = tk.Frame(self, bg=C["surface"])
        head.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(head, text=title, bg=C["surface"], fg=C["ink"], font=F().h2, anchor="w").pack(anchor="w")
        if subtitle:
            tk.Label(head, text=subtitle, bg=C["surface"], fg=C["muted"], font=F().small, anchor="w",
                     justify="left", wraplength=560).pack(anchor="w", pady=(2, 0))
        self.body = tk.Frame(self, bg=C["surface"])
        self.body.pack(fill="both", expand=True, padx=20, pady=8)
        self.error = tk.Label(self, text="", bg=C["surface"], fg=C["bad"], font=F().small, anchor="w",
                              justify="left", wraplength=560)
        self.error.pack(fill="x", padx=20)
        ttk.Separator(self).pack(fill="x", pady=(8, 0))
        foot = tk.Frame(self, bg=C["surface_alt"])
        foot.pack(fill="x")
        self.save_button = ttk.Button(foot, text="Сохранить", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right", padx=(6, 20), pady=12)
        ttk.Button(foot, text="Отмена", command=self.cancel).pack(side="right", pady=12)
        self.hint = tk.Label(foot, text="Enter — сохранить, Esc — отмена", bg=C["surface_alt"], fg=C["muted"],
                             font=F().caption)
        self.hint.pack(side="left", padx=20)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Return>", self._on_return)

    def _on_return(self, event):
        if isinstance(event.widget, tk.Text):
            return
        self.save()

    def field(self, row: int, column: int, label: str, widget: tk.Widget, *, hint: str | None = None,
              columnspan: int = 1) -> tk.Widget:
        cell = tk.Frame(self.body, bg=C["surface"])
        cell.grid(row=row, column=column, columnspan=columnspan, sticky="new", padx=(0, 14), pady=6)
        form_label(cell, label).pack(anchor="w", pady=(0, 3))
        widget.pack(in_=cell, fill="x")
        widget.lift()
        if hint:
            tk.Label(cell, text=hint, bg=C["surface"], fg=C["muted"], font=F().caption, anchor="w",
                     justify="left", wraplength=260).pack(anchor="w", pady=(2, 0))
        self.body.columnconfigure(column, weight=1, uniform="col")
        return widget

    def values(self) -> dict:
        return {}

    def show(self):
        self.update_idletasks()
        parent = self.app.root
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
        self.geometry(f"+{x}+{y}")
        self.minsize(w, h)
        self.deiconify()
        self._initial = self.values()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_first()
        self.wait_window()
        return self.result

    def focus_first(self):
        pass

    def set_error(self, text: str):
        self.error.configure(text=text, fg=C["bad"])

    def set_notice(self, text: str, warn: bool = False):
        """Нейтральное пояснение (не ошибка) под формой."""
        self.error.configure(text=text, fg=C["warn"] if warn else C["ink2"])

    def cancel(self):
        if self._initial is not None and self.values() != self._initial:
            if not messagebox.askyesno("Закрыть форму", "Введённые данные не сохранены. Закрыть без сохранения?",
                                       parent=self, icon="warning"):
                return
        self.destroy()

    def confirm_warnings(self, warnings: list[str]) -> bool:
        if not warnings:
            return True
        return messagebox.askyesno("Проверьте данные", "\n\n".join(warnings) + "\n\nСохранить запись?",
                                   parent=self, icon="warning")

    def save(self):
        if self._saving:
            return
        self._saving = True
        self.save_button.state(["disabled"])
        try:
            self.set_error("")
            if self.on_save():
                self.destroy()
                return
        except DatabaseError as exc:
            self.set_error(describe_error(exc))
        except Exception as exc:          # noqa: BLE001 — показать и записать в журнал
            self.app.log_exception(exc)
            self.set_error(describe_error(exc))
        self._saving = False
        if self.winfo_exists():
            self.save_button.state(["!disabled"])

    def on_save(self) -> bool:
        raise NotImplementedError


def _entry(parent, var: tk.StringVar, width: int = 14) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=var, width=width)


def _num_text(value) -> str:
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}".replace(".", ",")


# --- Производственная запись ---------------------------------------------------------

class ProductionDialog(FormDialog):
    def __init__(self, app, record: dict | None = None, defaults: dict | None = None):
        editing = record is not None
        super().__init__(app, "Изменить запись смены" if editing else "Новая запись смены",
                         "Вводите значения из подтверждённых журналов или измерений. "
                         "OEE пересчитывается сразу — проверьте результат до сохранения.")
        self.record = record
        defaults = defaults or {}
        self.equipment = {e["id"]: e for e in app.db.list_equipment()}
        self.v = {k: tk.StringVar() for k in ("work_date", "shift", "planned", "run", "total", "good", "cycle", "note")}
        self.picker = IdCombobox(self.body, width=30)
        items = [(e["id"], e["name"] + ("" if e["is_active"] else " (не используется)"))
                 for e in self.equipment.values() if e["is_active"] or (record and e["id"] == record["equipment_id"])]
        self.picker.set_items(items, keep=False)
        self.field(0, 0, "Оборудование *", self.picker, columnspan=2)
        self.date = DateEntry(self.body, self.v["work_date"])
        self.field(0, 2, "Дата смены *", self.date)
        shift = ttk.Combobox(self.body, textvariable=self.v["shift"], values=SHIFTS, state="readonly", width=12)
        self.field(1, 0, "Смена *", shift)
        self.field(1, 1, "Плановое время, мин *", _entry(self.body, self.v["planned"]),
                   hint="Время, когда оборудование должно было работать")
        run_cell = tk.Frame(self.body, bg=C["surface"])
        _entry(run_cell, self.v["run"]).pack(side="left", fill="x", expand=True)
        calc = ttk.Button(run_cell, text="Из простоев", style="Small.TButton", command=self.run_from_downtimes)
        calc.pack(side="left", padx=(4, 0))
        Tooltip(calc, "Время работы = плановое время − зарегистрированные простои этой смены")
        self.field(1, 2, "Время работы, мин *", run_cell)
        self.field(2, 0, "Произведено, шт. *", _entry(self.body, self.v["total"]))
        self.field(2, 1, "Годные, шт. *", _entry(self.body, self.v["good"]))
        self.field(2, 2, "Идеальный цикл, с/шт. *", _entry(self.body, self.v["cycle"]),
                   hint="Нормативное время выпуска одной единицы")
        self.field(3, 0, "Комментарий", _entry(self.body, self.v["note"], 40), columnspan=3)

        self.preview = tk.Frame(self.body, bg=C["surface_alt"], highlightbackground=C["border"], highlightthickness=1)
        self.preview.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.preview_values = {}
        for i, (key, name) in enumerate((("availability", "Доступность"), ("performance", "Производительность"),
                                         ("quality", "Качество"), ("oee", "OEE"))):
            cell = tk.Frame(self.preview, bg=C["surface_alt"])
            cell.grid(row=0, column=i, sticky="ew", padx=12, pady=8)
            self.preview.columnconfigure(i, weight=1)
            tk.Label(cell, text=name, bg=C["surface_alt"], fg=C["muted"], font=F().caption).pack(anchor="w")
            label = tk.Label(cell, text="—", bg=C["surface_alt"], fg=C["ink"], font=F().h2)
            label.pack(anchor="w")
            self.preview_values[key] = label
        self.preview_note = tk.Label(self.preview, text="", bg=C["surface_alt"], fg=C["muted"], font=F().small,
                                     anchor="w", justify="left", wraplength=560)
        self.preview_note.grid(row=1, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 8))

        if record:
            self.picker.set_id(record["equipment_id"])
            self.date.set_date(date.fromisoformat(record["work_date"]))
            self.v["shift"].set(record["shift"])
            for key, field in (("planned", "planned_min"), ("run", "run_min"), ("total", "total_count"),
                               ("good", "good_count"), ("cycle", "ideal_cycle_sec")):
                self.v[key].set(_num_text(record[field]))
            self.v["note"].set(record["note"])
            if record["source"] != SOURCE_MANUAL:
                self.set_notice(f"Запись создана источником «{SOURCE_LABELS[record['source']]}». "
                               "После изменения она останется помеченной как сгенерированная.")
        else:
            if defaults.get("equipment_id"):
                self.picker.set_id(defaults["equipment_id"])
            self.date.set_date(defaults.get("work_date") or date.today())
            self.v["shift"].set(defaults.get("shift") or SHIFTS[0])
            self.v["planned"].set(str(SHIFT_SCHEDULE[self.v["shift"].get()][1]))
            self._fill_cycle()
        self._last_equipment = self.picker.get_id()
        self.picker.bind("<<ComboboxSelected>>", lambda e: self._on_equipment())
        for var in self.v.values():
            var.trace_add("write", lambda *_: self.update_preview())
        self.update_preview()

    def values(self) -> dict:
        return {k: v.get() for k, v in self.v.items()} | {"equipment": self.picker.get_id()}

    def focus_first(self):
        self.picker.focus_set()

    def _fill_cycle(self):
        eq = self.equipment.get(self.picker.get_id())
        if eq and eq.get("ideal_cycle_sec"):
            self.v["cycle"].set(_num_text(eq["ideal_cycle_sec"]))

    def _on_equipment(self):
        previous = self.equipment.get(self._last_equipment)
        current_cycle = self.v["cycle"].get().strip()
        if not current_cycle or (previous and current_cycle == _num_text(previous.get("ideal_cycle_sec"))):
            self._fill_cycle()
        self._last_equipment = self.picker.get_id()
        self.update_preview()

    def run_from_downtimes(self):
        eq_id, day = self.picker.get_id(), self.date.get_date()
        try:
            planned = float(validation.parse_number(self.v["planned"].get(), "Плановое время"))
        except validation.FieldError as exc:
            self.set_error(str(exc))
            return
        if eq_id is None or day is None:
            self.set_error("Сначала выберите оборудование и дату смены.")
            return
        minutes, open_count = self.app.db.shift_downtime_minutes(eq_id, day, self.v["shift"].get())
        run = max(0.0, planned - minutes)
        self.v["run"].set(_num_text(round(run, 1)))
        note = f"Учтено простоев смены: {formatting.minutes(minutes)}."
        if open_count:
            note += f" Есть незавершённые простои ({open_count}) — они не учтены."
        if minutes > planned:
            note += " Простои больше планового времени — проверьте данные."
        self.set_notice(note, warn=bool(open_count) or minutes > planned)

    def update_preview(self):
        data, report = validation.validate_production(
            {k: v.get() for k, v in self.v.items()}, self.picker.get_id() or 0, date.today())
        if not data:
            for label in self.preview_values.values():
                label.configure(text="—", fg=C["ink"])
            first = report.errors[0] if report.errors else ""
            self.preview_note.configure(text=first, fg=C["muted"])
            return
        res = oee.calculate(data.planned_min, data.run_min, data.total_count, data.good_count, data.ideal_cycle_sec)
        for key in self.preview_values:
            value = getattr(res, key)
            over = value is not None and value > 1 + oee.EPS
            self.preview_values[key].configure(text=formatting.pct(value), fg=C["warn"] if over else C["ink"])
        defects = res.defect_count
        text = f"Брак: {formatting.integer(defects)} шт. · Потери доступности: {formatting.minutes(res.lost_time_min)}"
        if report.warnings:
            text = report.warnings[0]
        self.preview_note.configure(text=text, fg=C["warn"] if report.warnings else C["muted"])

    def on_save(self) -> bool:
        data, report = validation.validate_production({k: v.get() for k, v in self.v.items()},
                                                      self.picker.get_id(), date.today())
        if not data:
            self.set_error("\n".join(report.errors))
            return False
        if not self.confirm_warnings(report.warnings):
            return False
        if self.record:
            self.app.db.update_production(self.record["id"], data)
            self.result = self.record["id"]
            self.app.data_changed("Запись смены обновлена.")
        else:
            self.result = self.app.db.add_production(data)
            self.app.data_changed("Запись смены сохранена.")
        return True


# --- Простой -----------------------------------------------------------------------------

class DowntimeDialog(FormDialog):
    def __init__(self, app, record: dict | None = None, defaults: dict | None = None):
        super().__init__(app, "Изменить простой" if record else "Новый простой",
                         "Простои одного оборудования не могут пересекаться по времени. Если простой перешёл "
                         "через полночь, укажите дату окончания следующим днём. Дата смены и смена "
                         "подставляются по расписанию — их можно изменить.")
        self.record = record
        defaults = defaults or {}
        self.v = {k: tk.StringVar() for k in ("start_date", "start_time", "end_date", "end_time", "work_date",
                                              "shift", "reason", "comment")}
        self.ongoing = tk.BooleanVar(value=False)
        self.picker = IdCombobox(self.body, width=30)
        equipment = app.db.list_equipment()
        self.picker.set_items([(e["id"], e["name"]) for e in equipment
                               if e["is_active"] or (record and e["id"] == record["equipment_id"])], keep=False)
        self.field(0, 0, "Оборудование *", self.picker, columnspan=2)
        reasons = [r["name"] for r in app.db.list_reasons(active_only=True)]
        if record and record["reason"] not in reasons:
            reasons.append(record["reason"])
        self.reasons = set(reasons)
        self.field(0, 2, "Причина *", ttk.Combobox(self.body, textvariable=self.v["reason"], values=reasons,
                                                   state="readonly", width=26))
        self.start_date = DateEntry(self.body, self.v["start_date"], on_pick=self._start_changed)
        self.field(1, 0, "Начало: дата *", self.start_date)
        self.field(1, 1, "Начало: время (ЧЧ:ММ) *", _entry(self.body, self.v["start_time"], 8))
        ongoing = ttk.Checkbutton(self.body, text="Простой продолжается", variable=self.ongoing,
                                  style="Card.TCheckbutton", command=self._toggle_end)
        self.field(1, 2, "Статус", ongoing)
        self.end_date = DateEntry(self.body, self.v["end_date"])
        self.field(2, 0, "Окончание: дата *", self.end_date)
        self.end_time_entry = _entry(self.body, self.v["end_time"], 8)
        self.field(2, 1, "Окончание: время (ЧЧ:ММ) *", self.end_time_entry)
        self.next_day = ttk.Button(self.body, text="Окончание — следующим днём", style="Small.TButton",
                                   command=self._end_next_day)
        self.field(2, 2, " ", self.next_day)
        self.work_date = DateEntry(self.body, self.v["work_date"])
        self.field(3, 0, "Дата смены *", self.work_date)
        self.field(3, 1, "Смена *", ttk.Combobox(self.body, textvariable=self.v["shift"], values=SHIFTS,
                                                 state="readonly", width=12))
        self.duration = tk.Label(self.body, text="", bg=C["surface"], fg=C["ink"], font=F().h3, anchor="w")
        self.field(3, 2, "Длительность", self.duration)
        self.field(4, 0, "Комментарий", _entry(self.body, self.v["comment"], 50), columnspan=3)

        if record:
            start = parse_ts(record["start_at"])
            self.picker.set_id(record["equipment_id"])
            self.start_date.set_date(start.date())
            self.v["start_time"].set(start.strftime("%H:%M"))
            if record["end_at"]:
                end = parse_ts(record["end_at"])
                self.end_date.set_date(end.date())
                self.v["end_time"].set(end.strftime("%H:%M"))
            else:
                self.ongoing.set(True)
            self.work_date.set_date(date.fromisoformat(record["work_date"]))
            self.v["shift"].set(record["shift"])
            self.v["reason"].set(record["reason"])
            self.v["comment"].set(record["comment"])
            self._auto_shift = False
        else:
            now = datetime.now().replace(second=0, microsecond=0)
            start = defaults.get("start") or now - timedelta(minutes=15)
            if defaults.get("equipment_id"):
                self.picker.set_id(defaults["equipment_id"])
            self.start_date.set_date(start.date())
            self.v["start_time"].set(start.strftime("%H:%M"))
            self.end_date.set_date(start.date())
            self.v["end_time"].set((start + timedelta(minutes=15)).strftime("%H:%M"))
            self.v["reason"].set(UNCLASSIFIED_REASON if UNCLASSIFIED_REASON in self.reasons else reasons[0])
            self._auto_shift = True
            self._suggest_shift()
        self._toggle_end()
        for key in ("start_date", "start_time"):
            self.v[key].trace_add("write", lambda *_: self._start_changed())
        for key in ("end_date", "end_time"):
            self.v[key].trace_add("write", lambda *_: self.update_duration())
        for key in ("work_date", "shift"):
            self.v[key].trace_add("write", lambda *_: self._manual_shift())
        self.update_duration()

    def values(self) -> dict:
        return {k: v.get() for k, v in self.v.items()} | {"ongoing": self.ongoing.get(),
                                                          "equipment": self.picker.get_id()}

    def focus_first(self):
        self.picker.focus_set()

    def _manual_shift(self):
        if not getattr(self, "_setting_shift", False):
            self._auto_shift = False

    def _start(self) -> datetime | None:
        try:
            return datetime.combine(validation.parse_date(self.v["start_date"].get()),
                                    validation.parse_time(self.v["start_time"].get()))
        except validation.FieldError:
            return None

    def _suggest_shift(self):
        start = self._start()
        if start is None or not self._auto_shift:
            return
        work_date, shift = validation.shift_for(start)
        self._setting_shift = True
        self.work_date.set_date(work_date)
        self.v["shift"].set(shift)
        self._setting_shift = False

    def _start_changed(self):
        self._suggest_shift()
        self.update_duration()

    def _toggle_end(self):
        state = ["disabled"] if self.ongoing.get() else ["!disabled"]
        for widget in (self.end_date.entry, self.end_date.button, self.end_time_entry, self.next_day):
            widget.state(state)
        self.update_duration()

    def _end_next_day(self):
        start = self._start()
        if start:
            self.end_date.set_date(start.date() + timedelta(days=1))

    def update_duration(self):
        if self.ongoing.get():
            self.duration.configure(text="продолжается", fg=C["warn"])
            return
        start = self._start()
        try:
            end = datetime.combine(validation.parse_date(self.v["end_date"].get()),
                                   validation.parse_time(self.v["end_time"].get()))
        except validation.FieldError:
            end = None
        if start is None or end is None:
            self.duration.configure(text="—", fg=C["muted"])
        elif end <= start:
            self.duration.configure(text="окончание раньше начала", fg=C["bad"])
        else:
            self.duration.configure(text=formatting.minutes((end - start).total_seconds() / 60), fg=C["ink"])

    def on_save(self) -> bool:
        raw = {k: v.get() for k, v in self.v.items()} | {"ongoing": self.ongoing.get()}
        data, report = validation.validate_downtime(raw, self.picker.get_id(), datetime.now(), self.reasons)
        if not data:
            self.set_error("\n".join(report.errors))
            return False
        if not self.confirm_warnings(report.warnings):
            return False
        if self.record:
            self.app.db.update_downtime(self.record["id"], data)
            self.result = self.record["id"]
            self.app.data_changed("Простой обновлён.")
        else:
            self.result = self.app.db.add_downtime(data)
            self.app.data_changed("Простой добавлен в журнал.")
        return True


class CloseDowntimeDialog(FormDialog):
    def __init__(self, app, record: dict):
        start = parse_ts(record["start_at"])
        super().__init__(app, "Завершить простой",
                         f"«{record['equipment_name']}», начало {formatting.ru_datetime(start)}, "
                         f"причина: {record['reason']}.")
        self.record = record
        self.start = start
        self.v_date, self.v_time = tk.StringVar(), tk.StringVar()
        now = datetime.now()
        self.date = DateEntry(self.body, self.v_date)
        self.date.set_date(now.date())
        self.v_time.set(now.strftime("%H:%M"))
        self.field(0, 0, "Окончание: дата", self.date)
        self.field(0, 1, "Окончание: время (ЧЧ:ММ)", _entry(self.body, self.v_time, 8))

    def values(self) -> dict:
        return {"d": self.v_date.get(), "t": self.v_time.get()}

    def on_save(self) -> bool:
        try:
            end = datetime.combine(validation.parse_date(self.v_date.get(), "Дата окончания"),
                                   validation.parse_time(self.v_time.get(), "Время окончания"))
        except validation.FieldError as exc:
            self.set_error(str(exc))
            return False
        if end > datetime.now() + validation.FUTURE_TOLERANCE:
            self.set_error("Окончание простоя не может быть в будущем.")
            return False
        if end <= self.start:
            self.set_error("Окончание должно быть позже начала простоя.")
            return False
        self.app.db.close_downtime(self.record["id"], end)
        self.app.data_changed(f"Простой завершён: {formatting.minutes((end - self.start).total_seconds() / 60)}.")
        self.result = True
        return True


# --- Оборудование ------------------------------------------------------------------------

class EquipmentDialog(FormDialog):
    def __init__(self, app, record: dict | None = None):
        super().__init__(app, "Изменить оборудование" if record else "Новое оборудование",
                         "Идеальный цикл подставляется в новые записи смен по этому оборудованию.")
        self.record = record
        self.v = {k: tk.StringVar() for k in ("name", "area", "cycle", "note")}
        self.active = tk.BooleanVar(value=True)
        self.name_entry = _entry(self.body, self.v["name"], 36)
        self.field(0, 0, "Название *", self.name_entry, columnspan=2)
        self.field(1, 0, "Участок / цех", _entry(self.body, self.v["area"], 20))
        self.field(1, 1, "Идеальный цикл, с/шт.", _entry(self.body, self.v["cycle"], 10),
                   hint="Необязательно; используется по умолчанию")
        self.field(2, 0, "Примечание", _entry(self.body, self.v["note"], 36), columnspan=2)
        self.field(3, 0, "Статус", ttk.Checkbutton(self.body, text="Используется (доступно для новых записей)",
                                                   variable=self.active, style="Card.TCheckbutton"), columnspan=2)
        if record:
            self.v["name"].set(record["name"])
            self.v["area"].set(record["area"])
            self.v["cycle"].set(_num_text(record["ideal_cycle_sec"]))
            self.v["note"].set(record["note"])
            self.active.set(bool(record["is_active"]))

    def values(self) -> dict:
        return {k: v.get() for k, v in self.v.items()} | {"is_active": self.active.get()}

    def focus_first(self):
        self.name_entry.focus_set()

    def on_save(self) -> bool:
        data, report = validation.validate_equipment(self.values())
        if not data:
            self.set_error("\n".join(report.errors))
            return False
        if self.record:
            self.app.db.update_equipment(self.record["id"], data)
            self.result = self.record["id"]
            self.app.data_changed("Оборудование обновлено.")
        else:
            self.result = self.app.db.add_equipment(data)
            self.app.data_changed(f"Оборудование «{data.name}» добавлено.")
        return True


# --- Справочник причин --------------------------------------------------------------------

class ReasonsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Справочник причин простоев")
        self.configure(bg=C["surface"])
        self.transient(app.root)
        self.geometry("620x460")
        self.minsize(520, 360)
        tk.Label(self, text="Справочник причин простоев", bg=C["surface"], fg=C["ink"], font=F().h2).pack(
            anchor="w", padx=20, pady=(16, 2))
        tk.Label(self, text="Переименование меняет причину и в сохранённых простоях. Отключённая причина "
                            "не предлагается в новых записях, но остаётся в истории.",
                 bg=C["surface"], fg=C["muted"], font=F().small, wraplength=560, justify="left").pack(
            anchor="w", padx=20)
        bar = tk.Frame(self, bg=C["surface"])
        bar.pack(fill="x", padx=20, pady=10)
        ttk.Button(bar, text="Добавить…", style="Primary.TButton", command=self.add).pack(side="left")
        ttk.Button(bar, text="Переименовать…", command=self.rename).pack(side="left", padx=6)
        ttk.Button(bar, text="Включить / отключить", command=self.toggle).pack(side="left")
        self.table = DataTable(self, [
            Column("name", "Причина", 280, stretch=True),
            Column("status", "Статус", 120),
            Column("usage", "Простоев", 90, anchor="e", numeric=True),
        ], height=10)
        self.table.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.table.set_tags(lambda r: ("muted",) if not r["is_active"] else ())
        self.table.bind_open(self.rename)
        self.bind("<Escape>", lambda e: self.destroy())
        self.refresh()

    def refresh(self):
        rows = self.app.db.list_reasons()
        for r in rows:
            r["status"] = "системная" if r["is_system"] else ("используется" if r["is_active"] else "отключена")
        self.table.set_rows(rows)

    def _run(self, func, message: str):
        try:
            func()
        except DatabaseError as exc:
            messagebox.showerror("Справочник причин", describe_error(exc), parent=self)
            return
        self.refresh()
        self.app.data_changed(message)

    def add(self):
        name = simpledialog.askstring("Новая причина", "Название причины простоя:", parent=self)
        if name:
            self._run(lambda: self.app.db.add_reason(name), "Причина добавлена.")

    def rename(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Справочник причин", "Выберите причину в списке.", parent=self)
            return
        name = simpledialog.askstring("Переименовать", "Новое название:", initialvalue=row["name"], parent=self)
        if name and name.strip() != row["name"]:
            if row["usage"] and not messagebox.askyesno(
                    "Переименовать", f"Причина используется в {row['usage']} простоях; они тоже будут "
                                     "переименованы. Продолжить?", parent=self):
                return
            self._run(lambda: self.app.db.rename_reason(row["id"], name), "Причина переименована.")

    def toggle(self):
        row = self.table.selected()
        if not row:
            messagebox.showinfo("Справочник причин", "Выберите причину в списке.", parent=self)
            return
        self._run(lambda: self.app.db.set_reason_active(row["id"], not row["is_active"]), "Справочник обновлён.")
