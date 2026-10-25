"""Графики на tk.Canvas без внешних зависимостей.

Общие правила: тонкие линии (2 px), маркеры ≥ 8 px с белым кольцом, столбцы
не толще 24 px со скруглённым концом, светлая сетка, одна ось Y, легенда для
двух и более рядов, подсказка при наведении. Пропуски данных — разрывы линии,
а не нули.
"""
from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

from .. import formatting
from . import theme
from .theme import C

MARGIN_TOP = 34
MARGIN_RIGHT = 18
MARGIN_BOTTOM = 30


def F():
    return theme.FONTS


def compact(value: float) -> str:
    """Подписи оси: 950 / 12 тыс. / 1,2 млн."""
    a = abs(value)
    if a >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн".replace(".", ",").replace(",0 ", " ")
    if a >= 1_000:
        return f"{value / 1_000:.0f} тыс."
    return f"{value:.0f}"


def nice_step(span: float, target: int = 5) -> float:
    if span <= 0:
        return 1.0
    raw = span / target
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


def percent_scale(top: float, auto: bool) -> tuple[float, float, int]:
    """Верх шкалы долей, шаг и число знаков подписи.

    Обычный режим: 0–100%, расширяется только если есть значения выше 100%
    (их нельзя прятать). Режим auto: шкала по данным — для малых долей.
    """
    if auto:
        step = nice_step(max(top, 0.0001), 4)
        y_max = max(step, math.ceil(top / step - 1e-9) * step)
        digits = 0 if step >= 0.01 else (1 if step >= 0.001 else 2)
        return y_max, step, digits
    if top <= 1.0 + 1e-9:
        return 1.0, 0.2, 0
    step = 0.2 if top <= 1.6 else nice_step(top)
    return math.ceil(top * 1.02 / step) * step, step, 0


def bar_polygon(x0: float, y0: float, x1: float, y1: float, r: float, end: str) -> list[float]:
    """Прямоугольник со скруглённым концом данных (end = 'top' или 'right')."""
    r = max(0.0, min(r, abs(x1 - x0) / 2, abs(y1 - y0) / 2))
    if r < 1:
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    pts: list[float] = []
    steps = 4

    def arc(cx, cy, a0, a1):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend((cx + r * math.cos(a), cy - r * math.sin(a)))
    if end == "top":
        pts.extend((x0, y1))
        arc(x0 + r, y0 + r, 180, 90)
        arc(x1 - r, y0 + r, 90, 0)
        pts.extend((x1, y1))
    else:
        pts.extend((x0, y0))
        arc(x1 - r, y0 + r, 90, 0)
        arc(x1 - r, y1 - r, 0, -90)
        pts.extend((x0, y1))
    return pts


class Chart(tk.Canvas):
    """Базовый класс: перерисовка при изменении размера, пустое состояние, подсказки."""

    def __init__(self, parent, height: int = 240, empty_text: str = "Нет данных за выбранный период"):
        super().__init__(parent, bg=C["surface"], highlightthickness=0, height=height, bd=0)
        self.empty_text = empty_text
        self._hits: list[tuple[float, float, float, float, str]] = []
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._clear_hover())

    # Наследники реализуют has_data() и draw(w, h).
    def has_data(self) -> bool:
        return False

    def draw(self, width: int, height: int) -> None:
        raise NotImplementedError

    def redraw(self):
        self.delete("all")
        self._hits.clear()
        width, height = self.winfo_width(), self.winfo_height()
        if width < 40 or height < 40:
            return
        if not self.has_data():
            self.create_text(width / 2, height / 2, text=self.empty_text, fill=C["muted"], font=F().body,
                             justify="center", width=width - 40)
            return
        self.draw(width, height)

    def legend(self, items: Sequence[tuple[str, str]], x: float, y: float, line: bool = False):
        for name, color in items:
            if line:
                self.create_line(x, y, x + 16, y, fill=color, width=2, capstyle="round")
                self.create_oval(x + 4, y - 4, x + 12, y + 4, fill=color, outline=C["surface"], width=2)
                x += 22
            else:
                self.create_rectangle(x, y - 5, x + 10, y + 5, fill=color, outline="")
                x += 16
            text = self.create_text(x, y, text=name, anchor="w", fill=C["ink2"], font=F().small)
            x = self.bbox(text)[2] + 16

    def hit(self, x0, y0, x1, y1, text: str):
        self._hits.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), text))

    def _on_motion(self, event):
        self._clear_hover()
        text = self.hover(event.x, event.y)
        if text is None:
            for x0, y0, x1, y1, t in self._hits:
                if x0 - 3 <= event.x <= x1 + 3 and y0 - 3 <= event.y <= y1 + 3:
                    text = t
                    break
        if text:
            self._tooltip(event.x, event.y, text)

    def hover(self, x: float, y: float) -> str | None:
        """Переопределяется для подсказки по положению (например, перекрестие)."""
        return None

    def _clear_hover(self):
        self.delete("hover")

    def _tooltip(self, x: float, y: float, text: str):
        item = self.create_text(0, 0, text=text, anchor="nw", fill="#FFFFFF", font=F().small, tags="hover",
                                justify="left")
        x0, y0, x1, y1 = self.bbox(item)
        w, h = x1 - x0 + 16, y1 - y0 + 12
        px = x + 14 if x + 14 + w < self.winfo_width() else x - 14 - w
        py = max(4, min(y - h / 2, self.winfo_height() - h - 4))
        self.coords(item, px + 8, py + 6)
        rect = self.create_rectangle(px, py, px + w, py + h, fill=C["ink"], outline="", tags="hover")
        self.tag_lower(rect, item)


@dataclass
class Series:
    name: str
    color: str
    values: list[float | None]


class LineChart(Chart):
    """Динамика долей (0..1) по дням. None — нет данных (разрыв линии)."""

    def __init__(self, parent, height: int = 260, reference: tuple[float, str] | None = None,
                 auto_scale: bool = False, **kw):
        super().__init__(parent, height=height, **kw)
        self.labels: list[date] = []
        self.series: list[Series] = []
        self.reference = reference
        self.auto_scale = auto_scale      # True — шкала по данным (для малых долей, например брака)
        self._geo = None

    def set_data(self, labels: list[date], series: list[Series], reference: tuple[float, str] | None = None):
        self.labels, self.series = labels, series
        if reference is not None:
            self.reference = reference
        self.redraw()

    def has_data(self) -> bool:
        return any(v is not None for s in self.series for v in s.values)

    def draw(self, width, height):
        values = [v for s in self.series for v in s.values if v is not None]
        y_max, step, digits = percent_scale(max(values), self.auto_scale)
        left = 52
        x0, x1 = left, width - MARGIN_RIGHT
        y0, y1 = MARGIN_TOP, height - MARGIN_BOTTOM
        n = len(self.labels)

        def px(i):
            return (x0 + x1) / 2 if n == 1 else x0 + (x1 - x0) * i / (n - 1)

        def py(v):
            return y1 - (y1 - y0) * v / y_max

        for i in range(int(round(y_max / step)) + 1):
            tick = i * step
            y = py(tick)
            self.create_line(x0, y, x1, y, fill=C["grid"] if i else C["axis"])
            self.create_text(x0 - 8, y, text=f"{tick * 100:.{digits}f}%".replace(".", ","), anchor="e",
                             fill=C["muted"], font=F().caption)
        if y_max > 1.0 and not self.auto_scale:
            y = py(1.0)
            self.create_line(x0, y, x1, y, fill=C["warn"], dash=(4, 3))
            self.create_text(x1, y - 3, text="100%", anchor="se", fill=C["warn"], font=F().caption)
        if self.reference and self.reference[0] <= y_max:
            value, label = self.reference
            y = py(value)
            self.create_line(x0, y, x1, y, fill=C["border_strong"], dash=(2, 3))
            self.create_text(x0 + 4, y - 3, text=label, anchor="sw", fill=C["muted"], font=F().caption)

        label_w = 44
        every = max(1, math.ceil(n * label_w / max(1, x1 - x0)))
        for i, day in enumerate(self.labels):
            if i % every == 0 or i == n - 1 and n <= 2:
                self.create_text(px(i), y1 + 8, text=formatting.short_date(day), anchor="n", fill=C["muted"],
                                 font=F().caption)
        markers = n <= 45
        for s in self.series:
            points: list[tuple[float, float] | None] = [
                (px(i), py(v)) if v is not None else None for i, v in enumerate(s.values)]
            run: list[float] = []
            for i, p in enumerate(points + [None]):
                if p is None:
                    if len(run) >= 4:
                        self.create_line(*run, fill=s.color, width=2, capstyle="round", joinstyle="round")
                    run = []
                else:
                    run.extend(p)
            for i, p in enumerate(points):
                isolated = p is not None and (i == 0 or points[i - 1] is None) and (i == n - 1 or points[i + 1] is None)
                if p is not None and (markers or isolated):
                    self.create_oval(p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4, fill=s.color, outline=C["surface"], width=2)
        if len(self.series) >= 2:
            self.legend([(s.name, s.color) for s in self.series], x0, 14, line=True)
        self._geo = (x0, x1, y0, y1, n, px, py)

    def hover(self, x, y):
        if not self._geo:
            return None
        x0, x1, y0, y1, n, px, py = self._geo
        if not (x0 - 10 <= x <= x1 + 10 and y0 - 10 <= y <= y1 + 10) or n == 0:
            return None
        index = 0 if n == 1 else round((x - x0) / (x1 - x0) * (n - 1))
        index = max(0, min(n - 1, index))
        cx = px(index)
        self.create_line(cx, y0, cx, y1, fill=C["border_strong"], tags="hover")
        lines = [formatting.ru_date(self.labels[index])]
        for s in self.series:
            v = s.values[index]
            lines.append(f"{s.name}: {formatting.pct(v)}" if v is not None else f"{s.name}: нет данных")
            if v is not None:
                cy = py(v)
                self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=s.color, outline=C["surface"], width=2,
                                 tags="hover")
        return "\n".join(lines)


@dataclass
class Bar:
    label: str
    value: float
    value_text: str
    tooltip: str = ""


class HBarChart(Chart):
    """Горизонтальные столбцы (рейтинг): подпись слева, значение у конца столбца."""

    def __init__(self, parent, color: str = theme.SERIES[0], height: int = 240, **kw):
        super().__init__(parent, height=height, **kw)
        self.color = color
        self.bars: list[Bar] = []

    def set_data(self, bars: list[Bar]):
        self.bars = bars
        self.redraw()

    def has_data(self) -> bool:
        return any(b.value > 0 for b in self.bars)

    def draw(self, width, height):
        bars = [b for b in self.bars if b.value > 0]
        row = 30
        fits = max(1, int((height - 16) // row))
        hidden = max(0, len(bars) - fits)
        if hidden:
            fits = max(1, fits - 1)
            hidden = len(bars) - fits
            bars = bars[:fits]
        font = F().small
        label_w = min(width * 0.42, max(font.measure(b.label) for b in bars) + 12)
        value_w = max(font.measure(b.value_text) for b in bars) + 14
        x0, x1 = label_w, width - value_w - 6
        top = max(v.value for v in bars)
        for i, b in enumerate(bars):
            cy = 8 + i * row + row / 2
            label = b.label
            while font.measure(label) > label_w - 12 and len(label) > 4:
                label = label[:-2]
            if label != b.label:
                label = label.rstrip() + "…"
            self.create_text(label_w - 10, cy, text=label, anchor="e", fill=C["ink"], font=font)
            length = max(2.0, (x1 - x0) * b.value / top) if top > 0 else 0
            thickness = min(20, row - 10)
            self.create_polygon(bar_polygon(x0, cy - thickness / 2, x0 + length, cy + thickness / 2, 4, "right"),
                                fill=self.color, outline="")
            self.create_text(x0 + length + 6, cy, text=b.value_text, anchor="w", fill=C["ink2"], font=font)
            self.hit(0, cy - row / 2, width, cy + row / 2, b.tooltip or f"{b.label}: {b.value_text}")
        self.create_line(x0, 4, x0, 8 + len(bars) * row, fill=C["axis"])
        if hidden:
            self.create_text(label_w - 10, 8 + len(bars) * row + row / 2, anchor="e", fill=C["muted"],
                             font=F().caption, text=f"+ ещё {hidden} (см. таблицу)")


class GroupedBarChart(Chart):
    """Сгруппированные столбцы долей (0..1): категории × показатели."""

    def __init__(self, parent, height: int = 260, **kw):
        super().__init__(parent, height=height, **kw)
        self.categories: list[str] = []
        self.series: list[Series] = []

    def set_data(self, categories: list[str], series: list[Series]):
        self.categories, self.series = categories, series
        self.redraw()

    def has_data(self) -> bool:
        return any(v is not None for s in self.series for v in s.values)

    def draw(self, width, height):
        values = [v for s in self.series for v in s.values if v is not None]
        y_max, step, _digits = percent_scale(max(values), False)
        x0, x1 = 48, width - MARGIN_RIGHT
        y0, y1 = MARGIN_TOP, height - MARGIN_BOTTOM

        def py(v):
            return y1 - (y1 - y0) * v / y_max
        for i in range(int(round(y_max / step)) + 1):
            tick = i * step
            self.create_line(x0, py(tick), x1, py(tick), fill=C["grid"] if i else C["axis"])
            self.create_text(x0 - 8, py(tick), text=f"{tick * 100:.0f}%", anchor="e", fill=C["muted"],
                             font=F().caption)
        n = max(1, len(self.categories))
        band = (x1 - x0) / n
        k = len(self.series)
        bar = max(4.0, min(24.0, (band * 0.7 - 2 * (k - 1)) / k))
        group = bar * k + 2 * (k - 1)
        for ci, category in enumerate(self.categories):
            start = x0 + band * ci + (band - group) / 2
            self.create_text(x0 + band * (ci + 0.5), y1 + 8, text=category, anchor="n", fill=C["ink2"],
                             font=F().small)
            for si, s in enumerate(self.series):
                v = s.values[ci]
                bx = start + si * (bar + 2)
                if v is None:
                    continue
                top = py(v)
                if y1 - top >= 1:
                    self.create_polygon(bar_polygon(bx, top, bx + bar, y1, 4, "top"), fill=s.color, outline="")
                self.hit(bx, top, bx + bar, y1, f"{category}\n{s.name}: {formatting.pct(v)}")
        if y_max > 1.0:
            self.create_line(x0, py(1.0), x1, py(1.0), fill=C["warn"], dash=(4, 3))
        self.legend([(s.name, s.color) for s in self.series], x0, 14)


class StackedBarChart(Chart):
    """Столбцы с накоплением по дням (например, годные + брак)."""

    def __init__(self, parent, height: int = 240, **kw):
        super().__init__(parent, height=height, **kw)
        self.labels: list[date] = []
        self.stacks: list[Series] = []
        self.tooltip_fn: Callable[[int], str] | None = None

    def set_data(self, labels: list[date], stacks: list[Series], tooltip_fn: Callable[[int], str] | None = None):
        self.labels, self.stacks, self.tooltip_fn = labels, stacks, tooltip_fn
        self.redraw()

    def has_data(self) -> bool:
        return any((v or 0) > 0 for s in self.stacks for v in s.values)

    def draw(self, width, height):
        totals = [sum((s.values[i] or 0) for s in self.stacks) for i in range(len(self.labels))]
        step = nice_step(max(totals))
        y_max = max(step, math.ceil(max(totals) / step) * step)
        x0, x1 = 58, width - MARGIN_RIGHT
        y0, y1 = MARGIN_TOP, height - MARGIN_BOTTOM

        def py(v):
            return y1 - (y1 - y0) * v / y_max
        tick = 0.0
        while tick <= y_max + 1e-9:
            self.create_line(x0, py(tick), x1, py(tick), fill=C["grid"] if tick else C["axis"])
            self.create_text(x0 - 8, py(tick), text=compact(tick), anchor="e", fill=C["muted"], font=F().caption)
            tick += step
        n = max(1, len(self.labels))
        band = (x1 - x0) / n
        bar = max(1.0, min(24.0, band * 0.7))
        every = max(1, math.ceil(n * 44 / max(1, x1 - x0)))
        for i, day in enumerate(self.labels):
            cx = x0 + band * (i + 0.5)
            if i % every == 0:
                self.create_text(cx, y1 + 8, text=formatting.short_date(day), anchor="n", fill=C["muted"],
                                 font=F().caption)
            cumulative = 0.0
            last_drawn = None
            segments = []
            for s in self.stacks:
                v = s.values[i] or 0
                if v <= 0:
                    continue
                bottom = py(cumulative)
                cumulative += v
                segments.append((s, bottom, py(cumulative)))
            for index, (s, bottom, top) in enumerate(segments):
                gap = 2 if index > 0 and bar >= 4 else 0
                last = index == len(segments) - 1
                if bottom - gap - top < 0.5:
                    continue
                if last and bar >= 6:
                    self.create_polygon(bar_polygon(cx - bar / 2, top, cx + bar / 2, bottom - gap, 4, "top"),
                                        fill=s.color, outline="")
                else:
                    self.create_rectangle(cx - bar / 2, top, cx + bar / 2, bottom - gap, fill=s.color, outline="")
                last_drawn = top
            if totals[i] > 0:
                text = self.tooltip_fn(i) if self.tooltip_fn else formatting.ru_date(day)
                self.hit(cx - band / 2, last_drawn if last_drawn is not None else y1, cx + band / 2, y1, text)
        self.legend([(s.name, s.color) for s in self.stacks], x0, 14)


class Timeline(Chart):
    """Лента состояний оборудования за смену (симулятор)."""

    COLORS = {"RUNNING": C["good"], "STOPPED": C["bad"], "CONNECTION_LOST": C["warn"]}
    NAMES = {"RUNNING": "Работает", "STOPPED": "Остановлено", "CONNECTION_LOST": "Нет связи"}

    def __init__(self, parent, height: int = 92, **kw):
        super().__init__(parent, height=height, empty_text="Запустите симулятор, чтобы увидеть ленту состояний", **kw)
        self.segments: list[tuple[int, int, str]] = []
        self.length = 480
        self.start_label: Callable[[int], str] = lambda m: str(m)

    def set_data(self, segments: list[tuple[int, int, str]], length: int, start_label: Callable[[int], str]):
        self.segments, self.length, self.start_label = segments, length, start_label
        self.redraw()

    def has_data(self) -> bool:
        return bool(self.segments)

    def draw(self, width, height):
        x0, x1 = 12, width - 12
        y0, y1 = 30, height - 26
        self.create_rectangle(x0, y0, x1, y1, fill=C["surface_alt"], outline="")
        scale = (x1 - x0) / max(1, self.length)
        for start, end, state in self.segments:
            a, b = x0 + start * scale, x0 + end * scale
            if b - a < 1:
                b = a + 1
            self.create_rectangle(a, y0, b, y1, fill=self.COLORS[state], outline="")
            self.hit(a, y0, b, y1, f"{self.NAMES[state]}: {self.start_label(start)}–{self.start_label(end)} "
                                   f"({end - start} мин)")
        for hour in range(0, self.length + 1, 60):
            x = x0 + hour * scale
            self.create_line(x, y1, x, y1 + 4, fill=C["axis"])
            self.create_text(x, y1 + 6, text=self.start_label(hour), anchor="n", fill=C["muted"], font=F().caption)
        self.legend([(self.NAMES[s], c) for s, c in self.COLORS.items()], x0, 14)
