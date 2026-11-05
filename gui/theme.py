"""Colours and Qt styling for the logger window.

The categorical series palette is the validated 8-slot dark-mode order from the
data-visualisation reference palette. It passes, on the #1a1a19 surface:

    lightness band    all 8 inside L 0.48-0.67
    chroma floor      all 8 >= 0.1
    CVD separation    worst adjacent pair dE 8.4 (protan)
    normal vision     worst adjacent pair dE 19.3
    contrast          all 8 >= 3:1

Two rules it comes with, both honoured here:

  * Hues are assigned in fixed order and **never cycled**. A ninth channel does
    not get an invented or recycled hue -- it draws in neutral ink
    (`TEXT_SECONDARY`). See series_color().
  * A CVD separation in the 6-8 band is only legal alongside secondary encoding.
    Every plot panel carries its channel name and unit as a direct label, so
    identity never depends on colour at all; colour only helps the eye track a
    trace while scanning.

Values and labels wear text tokens, never the series colour -- a small coloured
rule beside the name carries identity instead.
"""

from __future__ import annotations

#: Chart surface and window chrome.
SURFACE = "#1a1a19"
SURFACE_RAISED = "#232322"
SURFACE_SUNKEN = "#121211"
BORDER = "#383835"

#: Ink.
TEXT_PRIMARY = "#f0efec"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#8a8a80"

#: Status palette -- reserved, never reused for a series.
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

#: Categorical series slots, in fixed order. Do not reorder or extend.
SERIES = (
    "#3987e5",  # 1 blue
    "#d95926",  # 2 orange
    "#199e70",  # 3 aqua
    "#c98500",  # 4 yellow
    "#d55181",  # 5 magenta
    "#008300",  # 6 green
    "#9085e9",  # 7 violet
    "#e66767",  # 8 red
)

#: Past this many plotted channels, panels draw in neutral ink rather than a
#: recycled hue. Identity still comes from each panel's direct label.
MAX_SERIES = len(SERIES)

#: Recessive grid.
GRID_ALPHA = 0.12

#: Line weight for data traces, per the mark spec.
LINE_WIDTH = 2


def series_color(index: int) -> str:
    """Colour for the nth plotted channel.

    Slots 0..7 take the categorical order. Beyond that the hue order is exhausted
    and cycling is not allowed, so further panels draw in neutral ink.
    """
    if 0 <= index < MAX_SERIES:
        return SERIES[index]
    return TEXT_SECONDARY


def state_color(state: str) -> str:
    """Status colour for a recorder lifecycle state."""
    return {
        "running": STATUS_GOOD,
        "connecting": STATUS_WARNING,
        "stopping": STATUS_WARNING,
        "error": STATUS_CRITICAL,
    }.get(state, TEXT_MUTED)


STYLESHEET = f"""
QWidget {{
    background: {SURFACE};
    color: {TEXT_PRIMARY};
    font-size: 12px;
}}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}
QLabel#heading {{
    color: {TEXT_SECONDARY};
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-size: 10px;
}}
QLabel#cost {{ color: {TEXT_SECONDARY}; }}
QLabel#costWarn {{ color: {STATUS_WARNING}; }}
QLabel#caveat {{ color: {TEXT_MUTED}; font-size: 11px; }}
QFrame#panel {{
    background: {SURFACE_RAISED};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QPushButton {{
    background: {SURFACE_RAISED};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {TEXT_MUTED}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; background: {SURFACE_SUNKEN}; }}
QPushButton:checked {{ border-color: {STATUS_GOOD}; color: {TEXT_PRIMARY}; }}
QPushButton#primary {{ border-color: {SERIES[0]}; }}
QComboBox, QLineEdit, QSpinBox {{
    background: {SURFACE_SUNKEN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {SERIES[0]};
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_RAISED};
    selection-background-color: {SERIES[0]};
    border: 1px solid {BORDER};
}}
QTreeWidget {{
    background: {SURFACE_SUNKEN};
    border: 1px solid {BORDER};
    border-radius: 4px;
    alternate-background-color: {SURFACE};
}}
QTreeWidget::item {{ padding: 2px 0; }}
QTreeWidget::item:selected {{ background: {BORDER}; }}
QHeaderView::section {{
    background: {SURFACE_RAISED};
    color: {TEXT_SECONDARY};
    border: 0;
    border-bottom: 1px solid {BORDER};
    padding: 4px;
}}
QStatusBar {{ background: {SURFACE_SUNKEN}; color: {TEXT_SECONDARY}; }}
QStatusBar::item {{ border: 0; }}
QToolTip {{
    background: {SURFACE_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}
QScrollBar:vertical {{ background: {SURFACE_SUNKEN}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""
