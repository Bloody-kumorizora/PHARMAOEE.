"""Главное окно: навигация, общий фильтр периода, строка состояния, фоновые операции."""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from .. import __version__, analytics
from ..config import SHIFTS, SOURCE_FILTERS, AppPaths, Settings
from ..db import Database, Filters, describe_error
from ..validation import FieldError, parse_date
from . import theme
from .theme import C
from .views import AnalyticsView, DashboardView, DowntimeView, EquipmentView, ProductionView
from .views_service import BackupView, DemoView, SettingsView
from .widgets import DateEntry, IdCombobox, StatusBar, Tooltip

log = logging.getLogger(__name__)

NAVIGATION = (
    ("dashboard", "Главная панель", DashboardView),
    ("production", "Производство", ProductionView),
    ("equipment", "Оборудование", EquipmentView),
    ("downtime", "Простои", DowntimeView),
    ("analytics", "Аналитика и отчёты", AnalyticsView),
    ("demo", "Демо и симулятор", DemoView),
    ("backup", "Резервные копии", BackupView),
    ("settings", "Настройки и справка", SettingsView),
)
ALL_EQUIPMENT = "Всё оборудование"
ALL_SHIFTS = "Все смены"


class NavItem(tk.Frame):
    def __init__(self, parent, text: str, hotkey: str, command):
        super().__init__(parent, bg=C["nav_bg"], cursor="hand2")
        self.command = command
        self.active = False
        self.bar = tk.Frame(self, bg=C["nav_bg"], width=3)
        self.bar.pack(side="left", fill="y")
        self.label = tk.Label(self, text=text, bg=C["nav_bg"], fg=C["nav_text"], font=theme.FONTS.nav,
                              anchor="w", padx=16, pady=9)
        self.label.pack(side="left", fill="x", expand=True)
        self.key = tk.Label(self, text=hotkey, bg=C["nav_bg"], fg=C["nav_muted"], font=theme.FONTS.caption, padx=10)
        self.key.pack(side="right")
        for widget in (self, self.label, self.key):
            widget.bind("<Button-1>", lambda e: self.command())
            widget.bind("<Enter>", lambda e: self._paint(hover=True))
            widget.bind("<Leave>", lambda e: self._paint())

    def set_active(self, active: bool):
        self.active = active
        self._paint()

    def _paint(self, hover: bool = False):
        bg = C["nav_active"] if self.active else (C["nav_hover"] if hover else C["nav_bg"])
        for widget in (self, self.label, self.key):
            widget.configure(bg=bg)
        self.label.configure(fg="#FFFFFF" if self.active else C["nav_text"])
        self.bar.configure(bg=C["nav_accent"] if self.active else bg)


class App:
    def __init__(self, root: tk.Tk, paths: AppPaths, db: Database, settings: Settings, notes: list[str] = ()):
        self.root, self.paths, self.db, self.settings = root, paths, db, settings
        theme.apply(root)
        root.title(f"PharmaOEE {__version__} — мониторинг OEE и простоев")
        root.geometry(settings.recent.get("geometry") or "1360x860")
        root.minsize(1024, 660)
        root.report_callback_exception = self._report_exception
        self._snapshot: analytics.Snapshot | None = None
        self._busy = False
        today = date.today()
        self._filters = Filters(today - timedelta(days=6), today, source=settings.default_source_filter)

        sidebar = tk.Frame(root, bg=C["nav_bg"], width=228)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=C["nav_bg"])
        brand.pack(fill="x", padx=18, pady=(20, 18))
        tk.Label(brand, text="PharmaOEE", bg=C["nav_bg"], fg="#FFFFFF", font=theme.FONTS.brand, anchor="w").pack(anchor="w")
        tk.Label(brand, text=f"v{__version__} · мониторинг OEE", bg=C["nav_bg"], fg=C["nav_muted"],
                 font=theme.FONTS.small, anchor="w").pack(anchor="w")
        self.nav: dict[str, NavItem] = {}
        for index, (key, label, _cls) in enumerate(NAVIGATION, start=1):
            item = NavItem(sidebar, label, f"Ctrl+{index}", lambda k=key: self.show_view(k))
            item.pack(fill="x")
            self.nav[key] = item
            root.bind_all(f"<Control-Key-{index}>", lambda e, k=key: self.show_view(k))
        foot = tk.Frame(sidebar, bg=C["nav_bg"])
        foot.pack(side="bottom", fill="x", padx=18, pady=16)
        tk.Label(foot, text="УЧЕБНЫЙ ПРОТОТИП", bg=C["nav_bg"], fg="#FFD27A", font=theme.FONTS.small_bold,
                 anchor="w").pack(anchor="w")
        tk.Label(foot, text="Не GMP-валидированная система.\nОборудование не подключено,\nданные — локальная SQLite.",
                 bg=C["nav_bg"], fg=C["nav_muted"], font=theme.FONTS.caption, justify="left", anchor="w").pack(anchor="w")

        main = ttk.Frame(root)
        main.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x", padx=theme.PAD + 6, pady=(16, 8))
        self.title_label = ttk.Label(header, text="", style="H1.TLabel")
        self.title_label.pack(anchor="w")
        self.subtitle_label = ttk.Label(header, text="", style="Subtitle.TLabel")
        self.subtitle_label.pack(anchor="w", pady=(2, 0))
        self.filter_bar = self._build_filters(main)
        self.status = StatusBar(main)
        self.status.pack(side="bottom", fill="x")
        self.status.right.configure(text=f"База: {self._short_path(db.path)}")
        Tooltip(self.status.right, str(db.path))
        self.content = ttk.Frame(main)
        self.content.pack(fill="both", expand=True, padx=theme.PAD + 6, pady=(4, 12))
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.views = {}
        for key, _label, cls in NAVIGATION:
            view = cls(self.content, self)
            view.grid(row=0, column=0, sticky="nsew")
            self.views[key] = view
        self.current = None
        root.bind_all("<F5>", lambda e: self.data_changed("Данные обновлены.", "info"))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._sync_filter_widgets()
        self.show_view("dashboard")
        for note in notes:
            self.notify(note, "warn", 15000)

    # --- Фильтры ------------------------------------------------------------------------------

    def _build_filters(self, parent) -> tk.Frame:
        bar = tk.Frame(parent, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1)
        bar.pack(fill="x", padx=theme.PAD + 6, pady=(0, 8))
        row1 = tk.Frame(bar, bg=C["surface"])
        row1.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(row1, text="Период", bg=C["surface"], fg=C["ink2"], font=theme.FONTS.small_bold).pack(side="left")
        self.f_from = DateEntry(row1, on_pick=self.apply_filters)
        self.f_from.pack(side="left", padx=(8, 4))
        tk.Label(row1, text="—", bg=C["surface"], fg=C["muted"]).pack(side="left")
        self.f_to = DateEntry(row1, on_pick=self.apply_filters)
        self.f_to.pack(side="left", padx=(4, 12))
        for entry in (self.f_from.entry, self.f_to.entry):
            entry.bind("<Return>", lambda e: self.apply_filters())
            entry.bind("<FocusOut>", lambda e: self.apply_filters(quiet=True))
        presets = (("Сегодня", 0), ("Вчера", -1), ("7 дней", 7), ("30 дней", 30), ("Этот месяц", "month"),
                   ("Всё время", "all"))
        for text, preset in presets:
            ttk.Button(row1, text=text, style="Chip.TButton", command=lambda p=preset: self.preset(p)).pack(
                side="left", padx=2)
        row2 = tk.Frame(bar, bg=C["surface"])
        row2.pack(fill="x", padx=12, pady=(4, 10))
        tk.Label(row2, text="Оборудование", bg=C["surface"], fg=C["ink2"], font=theme.FONTS.small_bold).pack(side="left")
        self.f_equipment = IdCombobox(row2, all_label=ALL_EQUIPMENT, width=30)
        self.f_equipment.pack(side="left", padx=(8, 14))
        tk.Label(row2, text="Смена", bg=C["surface"], fg=C["ink2"], font=theme.FONTS.small_bold).pack(side="left")
        self.f_shift = tk.StringVar(value=ALL_SHIFTS)
        shift = ttk.Combobox(row2, textvariable=self.f_shift, values=(ALL_SHIFTS,) + SHIFTS, state="readonly", width=11)
        shift.pack(side="left", padx=(8, 14))
        tk.Label(row2, text="Данные", bg=C["surface"], fg=C["ink2"], font=theme.FONTS.small_bold).pack(side="left")
        self.f_source = tk.StringVar()
        source = ttk.Combobox(row2, textvariable=self.f_source, values=list(SOURCE_FILTERS.values()),
                              state="readonly", width=24)
        source.pack(side="left", padx=(8, 14))
        Tooltip(source, "Ручной ввод — данные, введённые пользователем. Демо и симулятор — сгенерированные программой.")
        for combo in (self.f_equipment, shift, source):
            combo.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())
        self.filter_error = tk.Label(row2, text="", bg=C["surface"], fg=C["bad"], font=theme.FONTS.small)
        self.filter_error.pack(side="left")
        self.filter_info = tk.Label(row2, text="", bg=C["surface"], fg=C["muted"], font=theme.FONTS.small)
        self.filter_info.pack(side="right")
        Tooltip(self.filter_info, "Длина периода. Фильтр по дате смены; обе границы включены.")
        return bar

    def _sync_filter_widgets(self):
        f = self._filters
        self.f_from.set_date(f.date_from)
        self.f_to.set_date(f.date_to)
        self.refresh_equipment_filter()
        self.f_equipment.set_id(f.equipment_id)
        self.f_shift.set(f.shift or ALL_SHIFTS)
        self.f_source.set(SOURCE_FILTERS[f.source])
        self._update_filter_info()

    def refresh_equipment_filter(self):
        items = [(e["id"], e["name"] + ("" if e["is_active"] else " (не используется)"))
                 for e in self.db.list_equipment()]
        self.f_equipment.set_items(items)

    def _update_filter_info(self):
        f = self._filters
        if f.date_from and f.date_to:
            days = (f.date_to - f.date_from).days + 1
            self.filter_info.configure(text=f"{days} дн.")

    def preset(self, preset):
        today = date.today()
        if preset == 0:
            start = end = today
        elif preset == -1:
            start = end = today - timedelta(days=1)
        elif preset == "month":
            start, end = today.replace(day=1), today
        elif preset == "all":
            lo, hi = self.db.date_bounds()
            start, end = lo or today, max(hi or today, today)
        else:
            start, end = today - timedelta(days=preset - 1), today
        self.set_period(start, end)

    def set_period(self, start: date, end: date):
        self.f_from.set_date(start)
        self.f_to.set_date(end)
        self.apply_filters()

    def apply_filters(self, quiet: bool = False):
        try:
            start = parse_date(self.f_from.var.get(), "Начало периода")
            end = parse_date(self.f_to.var.get(), "Конец периода")
            if end < start:
                raise FieldError("Конец периода раньше начала.")
        except FieldError as exc:
            if not quiet:
                self.filter_error.configure(text=str(exc))
            return
        self.filter_error.configure(text="")
        shift = self.f_shift.get()
        source = next((k for k, v in SOURCE_FILTERS.items() if v == self.f_source.get()), "all")
        new = Filters(start, end, self.f_equipment.get_id(), None if shift == ALL_SHIFTS else shift, source)
        if new == self._filters:
            return
        self._filters = new
        self._update_filter_info()
        self.invalidate()

    def filters(self) -> Filters:
        return self._filters

    def filters_text(self) -> str:
        f = self._filters
        equipment = self.f_equipment.get() if f.equipment_id else ALL_EQUIPMENT
        return f"{equipment}; {f.shift or ALL_SHIFTS}; {SOURCE_FILTERS[f.source]}"

    # --- Данные ------------------------------------------------------------------------------

    def snapshot(self) -> analytics.Snapshot:
        if self._snapshot is None:
            self._snapshot = analytics.load_snapshot(self.db, self._filters)
        return self._snapshot

    def invalidate(self):
        """Сбрасывает кеш и обновляет текущий раздел; остальные обновятся при открытии."""
        self._snapshot = None
        for view in self.views.values():
            view.stale = True
        if self.current:
            self._refresh_current()

    def _refresh_current(self):
        view = self.views[self.current]
        try:
            view.refresh()
        except Exception as exc:     # noqa: BLE001 — ошибка не должна «ронять» окно
            self.log_exception(exc)
            self.notify("Не удалось обновить раздел: " + describe_error(exc), "bad", 0)

    def data_changed(self, message: str | None = None, kind: str = "good"):
        self.refresh_equipment_filter()
        if self._filters.equipment_id is not None and self.f_equipment.get_id() != self._filters.equipment_id:
            # Выбранное оборудование исчезло (удаление, восстановление копии) — сбрасываем фильтр.
            self._filters = Filters(self._filters.date_from, self._filters.date_to, None, self._filters.shift,
                                    self._filters.source)
        self.invalidate()
        if message:
            self.notify(message, kind)

    def notify(self, message: str, kind: str = "info", timeout_ms: int = 8000):
        self.status.show(message, kind, timeout_ms)

    def show_view(self, key: str, tab: str | None = None):
        if self.current:
            self.nav[self.current].set_active(False)
        self.current = key
        self.nav[key].set_active(True)
        view = self.views[key]
        self.title_label.configure(text=view.title)
        self.subtitle_label.configure(text=view.subtitle)
        if view.uses_filters:
            self.filter_bar.pack(fill="x", padx=theme.PAD + 6, pady=(0, 8), before=self.content)
        else:
            self.filter_bar.pack_forget()
        view.tkraise()
        if view.stale:
            self._refresh_current()
        if tab and hasattr(view, "select_tab"):
            view.select_tab(tab)

    def run_background(self, func, on_done, on_error=None, text: str = ""):
        """Длительная операция в отдельном потоке; интерфейс обновляется только из главного потока."""
        if self._busy:
            self.notify("Дождитесь завершения текущей операции.", "warn")
            return
        self._busy = True
        self.status.busy(True, text)
        self.root.configure(cursor="watch")
        results: queue.Queue = queue.Queue()

        def worker():
            try:
                results.put((True, func()))
            except BaseException as exc:     # noqa: BLE001 — результат передаётся в главный поток
                results.put((False, exc))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                ok, value = results.get_nowait()
            except queue.Empty:
                self.root.after(80, poll)
                return
            self._busy = False
            self.status.busy(False)
            self.root.configure(cursor="")
            if ok:
                on_done(value)
            else:
                self.log_exception(value)
                (on_error or (lambda e: messagebox.showerror("Ошибка", describe_error(e), parent=self.root)))(value)
                self.notify(describe_error(value), "bad")
        self.root.after(80, poll)

    def simulation_running(self) -> bool:
        return self.views["demo"].simulation_running

    # --- Служебное ----------------------------------------------------------------------------

    @staticmethod
    def _short_path(path: Path) -> str:
        text = str(path)
        return text if len(text) <= 60 else "…" + text[-58:]

    def open_path(self, path: Path):
        path = Path(path)
        if not path.exists():
            messagebox.showinfo("Открыть", f"Путь пока не существует:\n{path}", parent=self.root)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)            # noqa: S606 — открытие папки/файла пользователя
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Открыть", f"Не удалось открыть {path}: {exc}", parent=self.root)

    def log_exception(self, exc: BaseException):
        log.error("Ошибка: %s", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def _report_exception(self, exc_type, exc, tb):
        log.error("Необработанная ошибка интерфейса: %s", "".join(traceback.format_exception(exc_type, exc, tb)))
        messagebox.showerror("Непредвиденная ошибка",
                             f"{describe_error(exc)}\n\nПодробности записаны в журнал работы:\n{self.paths.log_path}",
                             parent=self.root)

    def on_close(self):
        demo_view = self.views["demo"]
        if demo_view.simulation_running:
            if not messagebox.askyesno("Выход", "Работает симулятор смены. Прервать сеанс (его записи будут удалены) "
                                                "и закрыть программу?", parent=self.root):
                return
            demo_view.abort_sim(ask=False)
        if self._busy and not messagebox.askyesno("Выход", "Операция ещё выполняется. Закрыть программу?",
                                                  parent=self.root):
            return
        try:
            if self.root.state() == "normal":
                self.settings.recent["geometry"] = self.root.geometry()
            self.settings.save(self.paths.settings_path)
        except (OSError, tk.TclError) as exc:
            log.warning("Настройки не сохранены: %s", exc)
        log.info("Программа закрыта")
        self.root.destroy()
