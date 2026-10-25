"""Переиспользуемые элементы интерфейса: карточки, KPI, таблицы, выбор даты, подсказки."""
from __future__ import annotations

import calendar
import tkinter as tk
from dataclasses import dataclass
from datetime import date
from tkinter import ttk
from typing import Callable

from .. import validation
from . import theme
from .theme import C

MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
          "Октябрь", "Ноябрь", "Декабрь")
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def F():
    return theme.FONTS


# --- Подсказки ---------------------------------------------------------------------

class Tooltip:
    """Всплывающая подсказка при наведении (с задержкой)."""

    def __init__(self, widget: tk.Widget, text: str | Callable[[], str], delay: int = 450):
        self.widget, self.text, self.delay = widget, text, delay
        self._after = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        text = self.text() if callable(self.text) else self.text
        if not text or self._tip:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(bg=C["ink"])
        tk.Label(tip, text=text, bg=C["ink"], fg="#FFFFFF", font=F().small, justify="left",
                 wraplength=340, padx=10, pady=7).pack()
        tip.update_idletasks()
        screen_w = tip.winfo_screenwidth()
        x = min(x, screen_w - tip.winfo_reqwidth() - 8)
        tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


# --- Контейнеры -------------------------------------------------------------------

class ScrollableFrame(ttk.Frame):
    """Вертикально прокручиваемая область; содержимое растягивается по ширине."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, bg=C["bg"], highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._update_region)
        self.canvas.bind("<Configure>", self._fit_width)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _update_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height()
        if needed and not self.vsb.winfo_ismapped():
            self.vsb.pack(side="right", fill="y")
        elif not needed and self.vsb.winfo_ismapped():
            self.vsb.pack_forget()
            self.canvas.yview_moveto(0)

    def _fit_width(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update_region()

    def _bind_wheel(self, on: bool):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
            self.canvas.bind_all("<Button-4>", lambda e: self._scroll(-1))
            self.canvas.bind_all("<Button-5>", lambda e: self._scroll(1))
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(seq)

    def _on_wheel(self, event):
        # Таблицы внутри прокручиваемой области прокручиваются сами.
        widget = event.widget
        if isinstance(widget, (ttk.Treeview, tk.Text, tk.Listbox)):
            return
        self._scroll(-1 if event.delta > 0 else 1)

    def _scroll(self, units: int):
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(units * 3, "units")


def wrap_on_resize(label: tk.Label, margin: int = 8) -> None:
    """Перенос строк по фактической ширине метки."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(120, e.width - margin)), add="+")


class Card(tk.Frame):
    """Поверхность с рамкой, заголовком и областью действий справа."""

    def __init__(self, parent, title: str | None = None, subtitle: str | None = None, padding: int = 14):
        super().__init__(parent, bg=C["surface"], highlightbackground=C["border"], highlightcolor=C["border"],
                         highlightthickness=1, bd=0)
        self.header = tk.Frame(self, bg=C["surface"])
        if title:
            self.header.pack(fill="x", padx=padding, pady=(padding - 2, 0))
            titles = tk.Frame(self.header, bg=C["surface"])
            titles.pack(side="left", fill="x", expand=True)
            self.title_label = tk.Label(titles, text=title, bg=C["surface"], fg=C["ink"], font=F().h2, anchor="w")
            self.title_label.pack(anchor="w")
            if subtitle:
                self.subtitle_label = tk.Label(titles, text=subtitle, bg=C["surface"], fg=C["muted"],
                                               font=F().small, anchor="w", justify="left")
                self.subtitle_label.pack(anchor="w", fill="x", pady=(1, 0))
                wrap_on_resize(self.subtitle_label)
        self.actions = tk.Frame(self.header, bg=C["surface"])
        self.actions.pack(side="right")
        self.body = tk.Frame(self, bg=C["surface"])
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)


class Banner(tk.Frame):
    """Информационная полоса: info / warn / bad / good."""

    KINDS = {"info": (C["primary_soft"], C["primary_hover"]), "warn": (C["warn_soft"], C["warn"]),
             "bad": (C["bad_soft"], C["bad"]), "good": (C["good_soft"], C["good"])}

    def __init__(self, parent, text: str = "", kind: str = "info"):
        super().__init__(parent, bd=0)
        self.bar = tk.Frame(self, width=4)
        self.bar.pack(side="left", fill="y")
        self.label = tk.Label(self, anchor="w", justify="left", font=F().body, padx=12, pady=8)
        self.label.pack(side="left", fill="both", expand=True)
        self.label.bind("<Configure>", lambda e: self.label.configure(wraplength=max(200, e.width - 24)))
        self.set(text, kind)

    def set(self, text: str, kind: str = "info"):
        bg, fg = self.KINDS[kind]
        self.configure(bg=bg)
        self.bar.configure(bg=fg)
        self.label.configure(text=text, bg=bg, fg=C["ink"])


# --- Показатели ---------------------------------------------------------------------

STATUS_COLORS = {"ok": C["good"], "bad": C["bad"], "over": C["warn"], "warn": C["warn"], None: C["border"]}


class KpiTile(tk.Frame):
    """Плитка показателя: название, значение, единица/пояснение, цветовая метка статуса."""

    def __init__(self, parent, label: str, help_text: str, hero: bool = False):
        super().__init__(parent, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1, bd=0)
        self.strip = tk.Frame(self, bg=C["border"], height=3)
        self.strip.pack(fill="x", side="top")
        inner = tk.Frame(self, bg=C["surface"])
        inner.pack(fill="both", expand=True, padx=14, pady=(10, 12))
        top = tk.Frame(inner, bg=C["surface"])
        top.pack(fill="x")
        tk.Label(top, text=label, bg=C["surface"], fg=C["ink2"], font=F().small_bold, anchor="w").pack(side="left")
        info = tk.Label(top, text="ⓘ", bg=C["surface"], fg=C["muted"], font=F().small, cursor="question_arrow")
        info.pack(side="right")
        Tooltip(info, help_text)
        Tooltip(self, help_text, delay=900)
        self.value = tk.Label(inner, text="—", bg=C["surface"], fg=C["ink"],
                              font=F().hero if hero else F().kpi, anchor="w")
        self.value.pack(fill="x", pady=(4, 0))
        self.sub = tk.Label(inner, text="", bg=C["surface"], fg=C["muted"], font=F().small, anchor="w",
                            justify="left")
        self.sub.pack(fill="x")

    def set(self, value: str, sub: str = "", status: str | None = None):
        self.value.configure(text=value, fg=C["warn"] if status == "over" else C["ink"])
        self.sub.configure(text=sub, fg=C["warn"] if status in ("over", "warn") else C["muted"])
        self.strip.configure(bg=STATUS_COLORS.get(status, C["border"]))


class MetricStrip(tk.Frame):
    """Строка второстепенных показателей с подписями и единицами измерения."""

    def __init__(self, parent, items: list[tuple[str, str, str]]):
        super().__init__(parent, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1)
        self.values: dict[str, tk.Label] = {}
        self.cells: list[tk.Frame] = []
        for index, (key, label, help_text) in enumerate(items):
            cell = tk.Frame(self, bg=C["surface"])
            lab = tk.Label(cell, text=label, bg=C["surface"], fg=C["muted"], font=F().small, anchor="w")
            lab.pack(anchor="w")
            value = tk.Label(cell, text="—", bg=C["surface"], fg=C["ink"], font=F().h2, anchor="w")
            value.pack(anchor="w", pady=(2, 0))
            Tooltip(cell, help_text)
            Tooltip(lab, help_text)
            self.values[key] = value
            self.cells.append(cell)
        self._columns = 0
        self.bind("<Configure>", self._relayout)

    def _relayout(self, event=None):
        width = self.winfo_width()
        columns = len(self.cells) if width > 900 else (3 if width > 420 else 2)
        if columns == self._columns:
            return
        self._columns = columns
        for i, cell in enumerate(self.cells):
            cell.grid(row=i // columns, column=i % columns, sticky="ew", padx=14, pady=10)
        for c in range(len(self.cells)):
            self.columnconfigure(c, weight=1 if c < columns else 0, uniform="m" if c < columns else "")

    def set(self, key: str, value: str, color: str | None = None):
        self.values[key].configure(text=value, fg=color or C["ink"])


# --- Таблица ------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int = 100
    anchor: str = "w"
    stretch: bool = False
    numeric: bool = False
    fmt: Callable | None = None


class DataTable(ttk.Frame):
    """Таблица с сортировкой по клику на заголовок, поиском и пустым состоянием."""

    def __init__(self, parent, columns: list[Column], *, height: int = 12, empty_text: str = "Нет записей",
                 key: str = "id", selectmode: str = "browse"):
        super().__init__(parent, style="Card.TFrame")
        self.columns = columns
        self.key = key
        self.empty_text = empty_text
        self.rows: list[dict] = []
        self.visible: list[dict] = []
        self._sort: tuple[str, bool] | None = None
        self._search = ""
        self._row_tags: Callable[[dict], tuple] | None = None
        self.tree = ttk.Treeview(self, columns=[c.key for c in columns], show="headings", height=height,
                                 selectmode=selectmode)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        for col in columns:
            self.tree.heading(col.key, text=col.title, anchor=col.anchor,
                              command=lambda k=col.key: self.sort_by(k))
            self.tree.column(col.key, width=col.width, minwidth=40, anchor=col.anchor, stretch=col.stretch)
        self.tree.tag_configure("odd", background="#FAFBFD")
        self.tree.tag_configure("warn", foreground=C["warn"])
        self.tree.tag_configure("bad", foreground=C["bad"])
        self.tree.tag_configure("generated", foreground=C["ink2"])
        self.tree.tag_configure("muted", foreground=C["muted"])
        self.empty = tk.Label(self, text=empty_text, bg=C["surface"], fg=C["muted"], font=F().body,
                              justify="center")
        self.tree.bind("<Home>", lambda e: self._jump(0))
        self.tree.bind("<End>", lambda e: self._jump(-1))

    def _jump(self, index: int):
        children = self.tree.get_children()
        if children:
            item = children[index]
            self.tree.selection_set(item)
            self.tree.see(item)

    def set_tags(self, func: Callable[[dict], tuple]):
        self._row_tags = func

    def set_rows(self, rows: list[dict], empty_text: str | None = None):
        if empty_text:
            self.empty_text = empty_text
        selected = self.selected_key()
        self.rows = rows
        self._render(selected)

    def search(self, text: str):
        self._search = text.strip().casefold()
        self._render(self.selected_key())

    def sort_by(self, key: str):
        ascending = not (self._sort and self._sort[0] == key and self._sort[1])
        self._sort = (key, ascending)
        for col in self.columns:
            arrow = (" ▲" if ascending else " ▼") if col.key == key else ""
            self.tree.heading(col.key, text=col.title + arrow)
        self._render(self.selected_key())

    def _display(self, row: dict) -> list:
        values = []
        for col in self.columns:
            raw = row.get(col.key)
            values.append(col.fmt(raw) if col.fmt else ("" if raw is None else raw))
        return values

    def _render(self, keep_selection=None):
        tree = self.tree
        tree.delete(*tree.get_children())
        rows = self.rows
        displays = [(row, self._display(row)) for row in rows]
        if self._search:
            displays = [(r, d) for r, d in displays if self._search in " ".join(map(str, d)).casefold()]
        if self._sort:
            key, ascending = self._sort
            col = next(c for c in self.columns if c.key == key)

            def sort_key(pair):
                raw = pair[0].get(key)
                if raw is None:
                    return (1, 0) if col.numeric else (1, "")
                return (0, raw) if col.numeric else (0, str(raw).casefold())
            missing = [p for p in displays if p[0].get(key) is None]
            present = sorted((p for p in displays if p[0].get(key) is not None), key=sort_key,
                             reverse=not ascending)
            displays = present + missing
        self.visible = [r for r, _ in displays]
        for index, (row, values) in enumerate(displays):
            tags = ("odd",) if index % 2 else ()
            if self._row_tags:
                tags = tags + tuple(self._row_tags(row))
            tree.insert("", "end", iid=str(row[self.key]), values=values, tags=tags)
        if keep_selection is not None and tree.exists(str(keep_selection)):
            tree.selection_set(str(keep_selection))
            tree.see(str(keep_selection))
        if displays:
            self.empty.place_forget()
        else:
            text = "Ничего не найдено по строке поиска" if self._search and rows else self.empty_text
            self.empty.configure(text=text)
            self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def selected_key(self):
        selection = self.tree.selection()
        if not selection:
            return None
        raw = selection[0]
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def selected(self) -> dict | None:
        key = self.selected_key()
        return next((r for r in self.rows if r[self.key] == key), None) if key is not None else None

    def bind_open(self, callback: Callable[[], None]):
        self.tree.bind("<Double-1>", lambda e: callback() if self.tree.identify_row(e.y) else None)
        self.tree.bind("<Return>", lambda e: callback())

    def bind_select(self, callback: Callable[[], None]):
        self.tree.bind("<<TreeviewSelect>>", lambda e: callback())


# --- Выбор даты -----------------------------------------------------------------------

class DateEntry(ttk.Frame):
    """Поле даты ДД.ММ.ГГГГ с календарём. Ввод с клавиатуры тоже поддерживается."""

    def __init__(self, parent, variable: tk.StringVar | None = None, width: int = 11,
                 on_pick: Callable[[], None] | None = None, style_bg: str = "Card"):
        super().__init__(parent, style=f"{style_bg}.TFrame" if style_bg == "Card" else "TFrame")
        self.var = variable or tk.StringVar()
        self.on_pick = on_pick
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        self.button = ttk.Button(self, text="▼", style="Icon.TButton", command=self.open_calendar,
                                 takefocus=False)
        self.button.pack(side="left", padx=(3, 0))
        Tooltip(self.button, "Выбрать дату в календаре")
        self.entry.bind("<Alt-Down>", lambda e: self.open_calendar())

    def get_date(self) -> date | None:
        try:
            return validation.parse_date(self.var.get())
        except validation.FieldError:
            return None

    def set_date(self, value: date | None):
        self.var.set(value.strftime("%d.%m.%Y") if value else "")

    def open_calendar(self):
        CalendarPopup(self, self.get_date() or date.today(), self._picked)

    def _picked(self, value: date):
        self.set_date(value)
        self.entry.focus_set()
        if self.on_pick:
            self.on_pick()


class CalendarPopup(tk.Toplevel):
    def __init__(self, anchor: tk.Widget, current: date, on_select: Callable[[date], None]):
        super().__init__(anchor)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=C["border_strong"])
        self.on_select = on_select
        self.selected = current
        self.year, self.month = current.year, current.month
        self.frame = tk.Frame(self, bg=C["surface"], padx=8, pady=8)
        self.frame.pack(padx=1, pady=1)
        self._build()
        self.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 2
        if y + self.winfo_reqheight() > self.winfo_screenheight():
            y = anchor.winfo_rooty() - self.winfo_reqheight() - 2
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        self.focus_force()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<FocusOut>", self._focus_out)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.bind("<Button-1>", self._click_outside, add="+")

    def _click_outside(self, event):
        x, y = event.x_root, event.y_root
        if not (self.winfo_rootx() <= x <= self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y <= self.winfo_rooty() + self.winfo_height()):
            self.destroy()

    def _focus_out(self, _event):
        self.after(50, self._check_focus)

    def _check_focus(self):
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or not str(focus).startswith(str(self)):
            if self.winfo_exists():
                self.destroy()

    def _build(self):
        for child in self.frame.winfo_children():
            child.destroy()
        head = tk.Frame(self.frame, bg=C["surface"])
        head.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 6))
        nav = dict(bg=C["surface"], fg=C["ink2"], bd=0, font=F().body_bold, activebackground=C["surface_alt"],
                   cursor="hand2", padx=8)
        tk.Button(head, text="‹", command=lambda: self._shift(-1), **nav).pack(side="left")
        tk.Label(head, text=f"{MONTHS[self.month - 1]} {self.year}", bg=C["surface"], fg=C["ink"],
                 font=F().body_bold).pack(side="left", expand=True)
        tk.Button(head, text="›", command=lambda: self._shift(1), **nav).pack(side="right")
        for i, name in enumerate(WEEKDAYS):
            tk.Label(self.frame, text=name, bg=C["surface"], fg=C["muted"] if i < 5 else C["bad"],
                     font=F().caption, width=4).grid(row=1, column=i)
        today = date.today()
        for r, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(self.year, self.month)):
            for c, day in enumerate(week):
                if not day:
                    continue
                value = date(self.year, self.month, day)
                selected = value == self.selected
                bg = C["primary"] if selected else (C["primary_soft"] if value == today else C["surface"])
                fg = "#FFFFFF" if selected else C["ink"]
                tk.Button(self.frame, text=str(day), width=3, bd=0, bg=bg, fg=fg, font=F().small,
                          activebackground=C["primary_soft"], cursor="hand2", relief="flat",
                          command=lambda v=value: self._choose(v)).grid(row=r + 2, column=c, padx=1, pady=1)
        tk.Button(self.frame, text="Сегодня", bd=0, bg=C["surface"], fg=C["primary"], font=F().small,
                  cursor="hand2", command=lambda: self._choose(today)).grid(row=9, column=0, columnspan=7, pady=(6, 0))

    def _shift(self, delta: int):
        month = self.month + delta
        self.year += (month - 1) // 12
        self.month = (month - 1) % 12 + 1
        self._build()

    def _choose(self, value: date):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.on_select(value)


# --- Строка состояния ------------------------------------------------------------------

class StatusBar(tk.Frame):
    COLORS = {"info": C["ink2"], "good": C["good"], "warn": C["warn"], "bad": C["bad"]}

    def __init__(self, parent):
        super().__init__(parent, bg=C["surface"], highlightbackground=C["border"], highlightthickness=1)
        self.dot = tk.Label(self, text="●", bg=C["surface"], fg=C["good"], font=F().small)
        self.dot.pack(side="left", padx=(12, 4), pady=4)
        self.message = tk.Label(self, text="Готово", bg=C["surface"], fg=C["ink2"], font=F().small, anchor="w")
        self.message.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(self, mode="indeterminate", length=140)
        self.right = tk.Label(self, text="", bg=C["surface"], fg=C["muted"], font=F().small)
        self.right.pack(side="right", padx=12)
        self._after = None

    def destroy(self):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        super().destroy()

    def show(self, text: str, kind: str = "info", timeout_ms: int = 8000):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        self.message.configure(text=text, fg=self.COLORS.get(kind, C["ink2"]))
        self.dot.configure(fg=self.COLORS.get(kind if kind != "info" else "good", C["good"]))
        if timeout_ms:
            self._after = self.after(timeout_ms, self._reset)

    def _reset(self):
        self._after = None
        self.show("Готово", "info", 0)

    def busy(self, on: bool, text: str = ""):
        if on:
            self.progress.pack(side="right", padx=8)
            self.progress.start(12)
            self.show(text or "Выполняется…", "info", 0)
        else:
            self.progress.stop()
            self.progress.pack_forget()


class IdCombobox(ttk.Combobox):
    """Выпадающий список «название → ID» только для выбора (без свободного ввода)."""

    def __init__(self, parent, *, all_label: str | None = None, width: int = 28, **kw):
        self.var = tk.StringVar()
        super().__init__(parent, textvariable=self.var, state="readonly", width=width, **kw)
        self.all_label = all_label
        self._ids: dict[str, int | None] = {}

    def set_items(self, items: list[tuple[int, str]], keep: bool = True):
        current = self.get_id() if keep else None
        self._ids = {}
        labels = []
        if self.all_label:
            self._ids[self.all_label] = None
            labels.append(self.all_label)
        for item_id, label in items:
            self._ids[label] = item_id
            labels.append(label)
        self.configure(values=labels)
        if current is not None and current in self._ids.values():
            self.set_id(current)
        elif self.var.get() not in self._ids:
            self.var.set(labels[0] if labels else "")

    def get_id(self) -> int | None:
        return self._ids.get(self.var.get())

    def set_id(self, item_id: int | None):
        for label, value in self._ids.items():
            if value == item_id:
                self.var.set(label)
                return
        if self.all_label and item_id is None:
            self.var.set(self.all_label)

    @property
    def is_empty(self) -> bool:
        return not any(v is not None for v in self._ids.values())


def form_label(parent, text: str, bg: str = C["surface"]) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg, fg=C["ink2"], font=F().small_bold, anchor="w")
