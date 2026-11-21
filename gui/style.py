"""The colour and Y range of one channel: the window a double click opens.

Both are choices a tuner makes once and wants kept: rpm is blue and runs 0 to
8000, load runs 0 to 10. So this edits them in one place and the window writes
the result into the preset, where it comes back at the next key-on.

The range is the scale's, not only the channel's. Channels that share a unit
share a Y scale -- that is what puts TL against LDSHK -- so a range given to one
is the range of all of them, and the dialog says which others it is about to
move rather than letting them jump.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

#: Common fixed Y ranges. A starting point -- min and max can be typed too.
PRESETS: tuple[tuple[str, float, float], ...] = (
    ("Engine speed 0 - 8000 rpm", 0, 8000),
    ("Load 0 - 10 ms/Umdr.", 0, 10),
    ("Boost 0 - 3000 mbar", 0, 3000),
    ("Boost 0 - 5 bar", 0, 5),
    ("Boost 0 - 300 kPa", 0, 300),
    ("Boost -1 - 2 bar (relative)", -1, 2),
    ("Air mass 0 - 1500 mg/stk", 0, 1500),
    ("Air flow 0 - 1000 kg/h", 0, 1000),
    ("Air flow 0 - 300 g/s", 0, 300),
    ("Lambda 0.6 - 1.4", 0.6, 1.4),
    ("AFR 8 - 20", 8, 20),
    ("Temperature 0 - 150 °C", 0, 150),
    ("EGT 0 - 1000 °C", 0, 1000),
    ("Timing -10 - 50 °", -10, 50),
    ("Knock retard -15 - 0 °", -15, 0),
    ("Throttle 0 - 90 °", 0, 90),
    ("Percent 0 - 100 %", 0, 100),
    ("Volts 0 - 16 V", 0, 16),
)


class StyleDialog(QtWidgets.QDialog):
    """Pick a colour and a Y range for one channel."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        label: str,
        unit: str = "",
        description: str = "",
        color: str = "",
        span: tuple[float, float] | None = None,
        data_span: tuple[float, float] | None = None,
        shared_with: list[str] | None = None,
        taken: set[str] | None = None,
        remembered: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{label} - colour and range")
        self._color = color or theme.SERIES[0]
        self._taken = {c.lower() for c in (taken or set())}
        self._swatches: dict[str, QtWidgets.QPushButton] = {}

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(14, 14, 14, 12)
        box.setSpacing(10)

        title = QtWidgets.QLabel(
            f"<b>{label}</b>"
            + (f'  <span style="color:{theme.TEXT_MUTED}">{unit}</span>' if unit else "")
        )
        box.addWidget(title)
        if description:
            about = QtWidgets.QLabel(description)
            about.setObjectName("caveat")
            about.setWordWrap(True)
            box.addWidget(about)

        # -- colour --------------------------------------------------------
        heading = QtWidgets.QLabel("Colour")
        heading.setObjectName("heading")
        box.addWidget(heading)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(6)
        for n, swatch in enumerate(theme.SERIES_EXTENDED):
            button = QtWidgets.QPushButton()
            button.setFixedSize(34, 26)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            used = swatch.lower() in self._taken and swatch.lower() != self._color.lower()
            button.setToolTip(
                f"{swatch}  (another drawn channel has it)" if used else swatch
            )
            button.clicked.connect(lambda _=False, c=swatch: self.set_color(c))
            grid.addWidget(button, n // 8, n % 8)
            self._swatches[swatch.lower()] = button
        box.addLayout(grid)

        other_row = QtWidgets.QHBoxLayout()
        self.color_label = QtWidgets.QLabel()
        other_row.addWidget(self.color_label)
        other_row.addStretch(1)
        other = QtWidgets.QPushButton("Other colour...")
        other.clicked.connect(self._pick_other)
        other_row.addWidget(other)
        box.addLayout(other_row)

        # -- range ---------------------------------------------------------
        heading = QtWidgets.QLabel("Y range")
        heading.setObjectName("heading")
        box.addWidget(heading)

        self.auto_radio = QtWidgets.QRadioButton("Auto: fit what is on screen")
        self.fixed_radio = QtWidgets.QRadioButton("Fixed:")
        box.addWidget(self.auto_radio)

        fixed_row = QtWidgets.QHBoxLayout()
        fixed_row.setSpacing(6)
        fixed_row.addWidget(self.fixed_radio)
        self.low_spin = self._spin()
        self.high_spin = self._spin()
        fixed_row.addWidget(self.low_spin)
        fixed_row.addWidget(QtWidgets.QLabel("to"))
        fixed_row.addWidget(self.high_spin)
        if unit:
            unit_label = QtWidgets.QLabel(unit)
            unit_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            fixed_row.addWidget(unit_label)
        fixed_row.addStretch(1)
        box.addLayout(fixed_row)

        self.common = QtWidgets.QComboBox()
        self.common.addItem("Common ranges...", None)
        for text, low, high in PRESETS:
            self.common.addItem(text, (low, high))
        self.common.activated.connect(self._on_common)
        box.addWidget(self.common)

        notes = []
        if shared_with:
            notes.append(
                f"Shared with {', '.join(shared_with)}: channels in the same unit "
                "are on one scale, so this is their range too."
            )
        if not remembered:
            notes.append(
                "This channel is not part of the preset, so the choice is kept "
                "with this log file only."
            )
        for text in notes:
            note = QtWidgets.QLabel(text)
            note.setObjectName("caveat")
            note.setWordWrap(True)
            box.addWidget(note)

        self.error_label = QtWidgets.QLabel()
        self.error_label.setStyleSheet(f"color: {theme.STATUS_WARNING};")
        self.error_label.setVisible(False)
        box.addWidget(self.error_label)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

        # What the spin boxes start on: the fixed range if there is one, else
        # what the data actually spans, so "Fixed" begins from something sane
        # rather than from 0 to 0.
        start = span or data_span or (0.0, 1.0)
        self.low_spin.setValue(start[0])
        self.high_spin.setValue(start[1])
        (self.fixed_radio if span is not None else self.auto_radio).setChecked(True)
        # Typing a number means you want it used.
        self.low_spin.valueChanged.connect(lambda _: self.fixed_radio.setChecked(True))
        self.high_spin.valueChanged.connect(lambda _: self.fixed_radio.setChecked(True))
        self.set_color(self._color)

    @staticmethod
    def _spin() -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(-1e9, 1e9)
        spin.setDecimals(3)
        spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setMinimumWidth(96)
        spin.setKeyboardTracking(False)
        return spin

    # -- colour ------------------------------------------------------------

    def set_color(self, color: str) -> None:
        self._color = color.lower()
        for swatch, button in self._swatches.items():
            chosen = swatch == self._color
            button.setStyleSheet(
                f"background: {swatch}; border-radius: 4px; border: "
                + (f"3px solid {theme.TEXT_PRIMARY};" if chosen
                   else f"1px solid {theme.BORDER};")
            )
        self.color_label.setText(
            f'<span style="color:{self._color}">&#9632;&#9632;&#9632;</span>  '
            f'<span style="color:{theme.TEXT_SECONDARY}">{self._color}</span>'
        )

    def _pick_other(self) -> None:
        picked = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(self._color), self, "Trace colour"
        )
        if picked.isValid():
            self.set_color(picked.name())

    def color(self) -> str:
        return self._color

    # -- range -------------------------------------------------------------

    def _on_common(self, index: int) -> None:
        span = self.common.itemData(index)
        if span:
            self.set_span(tuple(span))
        self.common.setCurrentIndex(0)

    def set_span(self, span: tuple[float, float] | None) -> None:
        if span is None:
            self.auto_radio.setChecked(True)
            return
        self.low_spin.setValue(span[0])
        self.high_spin.setValue(span[1])
        self.fixed_radio.setChecked(True)

    def span(self) -> tuple[float, float] | None:
        """The fixed range, or None for auto."""
        if self.auto_radio.isChecked():
            return None
        return float(self.low_spin.value()), float(self.high_spin.value())

    def _accept(self) -> None:
        span = self.span()
        if span is not None and span[1] <= span[0]:
            self.error_label.setText("The top of the range has to be above the bottom.")
            self.error_label.setVisible(True)
            return
        self.accept()
