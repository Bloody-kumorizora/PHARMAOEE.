"""Дизайн-токены (цвета, шрифты, отступы) и стили ttk.

Палитра: глубокий синий для навигации, нейтральные поверхности, один акцент
(синий) и зарезервированные статусные цвета. Цвета графиков взяты из
проверенной на различимость при дальтонизме категориальной палитры.
"""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

C = {
    "bg": "#EEF2F7",
    "surface": "#FFFFFF",
    "surface_alt": "#F5F7FA",
    "border": "#D8DFE9",
    "border_strong": "#BAC6D6",
    "ink": "#14233A",
    "ink2": "#4A5A70",
    "muted": "#78879C",
    "primary": "#1F6FD1",
    "primary_hover": "#185CB0",
    "primary_soft": "#E4EEFB",
    "nav_bg": "#0E2442",
    "nav_hover": "#16335A",
    "nav_active": "#1C3F6B",
    "nav_text": "#B9C8DC",
    "nav_muted": "#7F95B3",
    "nav_accent": "#5AB0FF",
    "good": "#1E7F4F",
    "good_soft": "#E2F3E9",
    "warn": "#9A6200",
    "warn_soft": "#FFF3D6",
    "bad": "#B83227",
    "bad_soft": "#FCE7E4",
    "grid": "#E7ECF2",
    "axis": "#AEB9C8",
}

#: Категориальная палитра графиков (фиксированный порядок слотов).
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
#: Назначение слотов показателям OEE — неизменно на всех графиках.
METRIC_COLORS = {"oee": SERIES[0], "availability": SERIES[1], "performance": SERIES[2], "quality": SERIES[3]}
GOOD_COLOR = "#2a78d6"
DEFECT_COLOR = "#e34948"

PAD = 16          # базовый внешний отступ
GAP = 12          # промежуток между блоками

#: Шрифты приложения; заполняется в apply().
FONTS: "Fonts | None" = None

_SANS = ("Segoe UI", "Inter", "Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans", "Liberation Sans",
         "Helvetica", "Arial")
_MONO = ("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Courier New")


class Fonts:
    """Типографическая шкала. Семейство подбирается из установленных в системе."""

    def __init__(self, root: tk.Misc):
        families = set(tkfont.families(root))
        sans = next((f for f in _SANS if f in families), "TkDefaultFont")
        mono = next((f for f in _MONO if f in families), "TkFixedFont")
        semibold = "Segoe UI Semibold" if "Segoe UI Semibold" in families and sans == "Segoe UI" else None
        self.family = sans

        def make(size, weight="normal", family=None):
            return tkfont.Font(root, family=family or sans, size=size, weight=weight)

        self.body = make(10)
        self.body_bold = make(10, "bold")
        self.small = make(9)
        self.small_bold = make(9, "bold")
        self.caption = make(8)
        self.h1 = make(17, "bold")
        self.h2 = make(12, "bold")
        self.h3 = make(10, "bold")
        self.kpi = make(20, "normal", semibold) if semibold else make(20, "bold")
        self.hero = make(34, "normal", semibold) if semibold else make(34, "bold")
        self.nav = make(10)
        self.brand = make(15, "bold")
        self.mono = tkfont.Font(root, family=mono, size=9)


def apply(root: tk.Tk) -> Fonts:
    global FONTS
    fonts = FONTS = Fonts(root)
    root.configure(bg=C["bg"])
    # Шрифты по умолчанию задаются через именованные шрифты Tk: опция *Font перекрыла бы стили ttk.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(name, root).configure(family=fonts.family, size=10)
        except tk.TclError:
            pass
    root.option_add("*TCombobox*Listbox.font", fonts.body)
    root.option_add("*TCombobox*Listbox.selectBackground", C["primary"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    row_height = fonts.body.metrics("linespace") + 12

    style.configure(".", background=C["bg"], foreground=C["ink"], font=fonts.body,
                    bordercolor=C["border"], lightcolor=C["surface"], darkcolor=C["border"],
                    troughcolor=C["surface_alt"], focuscolor=C["primary"], selectbackground=C["primary"],
                    selectforeground="#FFFFFF", insertcolor=C["ink"])
    style.configure("TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["surface"])
    style.configure("TLabel", background=C["bg"], foreground=C["ink"])
    for name, bg in (("Card", C["surface"]), ("Alt", C["surface_alt"])):
        style.configure(f"{name}.TLabel", background=bg, foreground=C["ink"])
        style.configure(f"{name}.Muted.TLabel", background=bg, foreground=C["muted"], font=fonts.small)
        style.configure(f"{name}.Secondary.TLabel", background=bg, foreground=C["ink2"])
        style.configure(f"{name}.H2.TLabel", background=bg, foreground=C["ink"], font=fonts.h2)
        style.configure(f"{name}.H3.TLabel", background=bg, foreground=C["ink"], font=fonts.h3)
        style.configure(f"{name}.Error.TLabel", background=bg, foreground=C["bad"], font=fonts.small)
        style.configure(f"{name}.TCheckbutton", background=bg, foreground=C["ink"])
        style.configure(f"{name}.TRadiobutton", background=bg, foreground=C["ink"])
        style.map(f"{name}.TCheckbutton", background=[("active", bg)])
        style.map(f"{name}.TRadiobutton", background=[("active", bg)])
    style.configure("H1.TLabel", font=fonts.h1, foreground=C["ink"])
    style.configure("Subtitle.TLabel", foreground=C["ink2"])
    style.configure("Muted.TLabel", foreground=C["muted"], font=fonts.small)
    style.configure("Error.TLabel", foreground=C["bad"], font=fonts.small)

    # Кнопки: обычная (контурная), основная (заливка), опасная и «тихая».
    style.configure("TButton", background=C["surface"], foreground=C["ink"], bordercolor=C["border_strong"],
                    lightcolor=C["surface"], darkcolor=C["surface"], padding=(12, 5), relief="raised",
                    focusthickness=1, focuscolor=C["primary_soft"])
    style.map("TButton", background=[("disabled", C["surface_alt"]), ("pressed", C["primary_soft"]),
                                     ("active", C["surface_alt"])],
              lightcolor=[("pressed", C["primary_soft"]), ("active", C["surface_alt"])],
              darkcolor=[("pressed", C["primary_soft"]), ("active", C["surface_alt"])],
              relief=[("pressed", "raised")],
              foreground=[("disabled", C["muted"])], bordercolor=[("focus", C["primary"]), ("disabled", C["border"])])
    style.configure("Primary.TButton", background=C["primary"], foreground="#FFFFFF", bordercolor=C["primary"],
                    lightcolor=C["primary"], darkcolor=C["primary"], font=fonts.body_bold)
    style.map("Primary.TButton", background=[("disabled", C["border"]), ("pressed", C["primary_hover"]),
                                             ("active", C["primary_hover"])],
              lightcolor=[("disabled", C["border"]), ("active", C["primary_hover"])],
              darkcolor=[("disabled", C["border"]), ("active", C["primary_hover"])],
              foreground=[("disabled", C["surface"])], bordercolor=[("focus", C["ink"])])
    style.configure("Danger.TButton", foreground=C["bad"])
    style.map("Danger.TButton", background=[("active", C["bad_soft"]), ("pressed", C["bad_soft"])])
    style.configure("Small.TButton", padding=(8, 2), font=fonts.small, width=0)
    style.configure("Icon.TButton", padding=(6, 1), width=0, font=fonts.caption, foreground=C["ink2"])
    style.configure("Chip.TButton", padding=(10, 3), font=fonts.small, width=0, background=C["surface_alt"],
                    lightcolor=C["surface_alt"], darkcolor=C["surface_alt"], bordercolor=C["border"])

    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=C["surface"], background=C["surface"],
                        bordercolor=C["border_strong"], lightcolor=C["surface"], darkcolor=C["surface"],
                        padding=(6, 4), arrowcolor=C["ink2"])
        style.map(widget, bordercolor=[("focus", C["primary"]), ("invalid", C["bad"])],
                  lightcolor=[("focus", C["primary"])],
                  fieldbackground=[("readonly", C["surface"]), ("disabled", C["surface_alt"])],
                  foreground=[("disabled", C["muted"])])
    style.configure("Invalid.TEntry", bordercolor=C["bad"], lightcolor=C["bad"])

    style.configure("Treeview", background=C["surface"], fieldbackground=C["surface"], foreground=C["ink"],
                    rowheight=row_height, bordercolor=C["border"], borderwidth=0, font=fonts.body)
    style.map("Treeview", background=[("selected", C["primary"])], foreground=[("selected", "#FFFFFF")])
    style.configure("Treeview.Heading", background=C["surface_alt"], foreground=C["ink2"], font=fonts.small_bold,
                    bordercolor=C["border"], lightcolor=C["surface_alt"], darkcolor=C["border"], relief="flat",
                    padding=(6, 6))
    style.map("Treeview.Heading", background=[("active", C["primary_soft"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=C["bg"], foreground=C["ink2"], padding=(14, 7),
                    bordercolor=C["bg"], lightcolor=C["bg"], font=fonts.body)
    style.map("TNotebook.Tab", background=[("selected", C["surface"]), ("active", C["surface_alt"])],
              foreground=[("selected", C["primary"])], lightcolor=[("selected", C["surface"])],
              bordercolor=[("selected", C["border"])])

    style.configure("TProgressbar", background=C["primary"], troughcolor=C["surface_alt"],
                    bordercolor=C["border"], lightcolor=C["primary"], darkcolor=C["primary"])
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=C["surface_alt"], troughcolor=C["surface"],
                        bordercolor=C["surface"], lightcolor=C["surface_alt"], darkcolor=C["surface_alt"],
                        arrowcolor=C["muted"], gripcount=0, arrowsize=12)
        style.map(f"{orient}.TScrollbar", background=[("active", C["border"])])
    style.configure("TSeparator", background=C["border"])
    style.configure("TCheckbutton", background=C["bg"])
    style.configure("TLabelframe", background=C["surface"])
    return fonts


def is_windows() -> bool:
    return sys.platform.startswith("win")
