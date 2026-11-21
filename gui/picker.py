"""Choosing what to log, and what to draw: the picker and the window around it.

244 channels is too many to scroll blindly, and they are not equal: a page-0x00
channel costs one wire byte, while the first channel on any other page also costs
a latch byte and two parity switches. Since throughput is the binding constraint,
the picker shows what a selection costs and what sample rate it implies, updating
as you tick boxes.

Each channel has two ticks, because logging and drawing are different decisions.
The first puts it in the CSV. The second puts it on the chart. KLINFO has to be
logged for the per-cylinder knock columns to exist, and drawn it is a cylinder
counter stepping 0-1-2-3 forty times a second -- so it is logged and not drawn,
and the default for a newly ticked channel follows that rule: quantities get a
trace, codes and status bytes do not.

The choice is made BEFORE connecting, in ``ChannelDialog``. Changing the channel
set mid-session reallocates the buffer and starts a different CSV layout, which
is how a log ends up with a pull split across two files; deciding first means
one session is one file with one set of columns.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets

from mcmess.channels import Channel, ChannelSet
from mcmess.derived import DERIVATIONS, DERIVED_COLUMNS, expand_selection
from mcmess.profile import (
    PROFILE_DIR,
    Profile,
    ProfileError,
    belongs_on_graph,
    load_profile,
    save_profile,
)
from mcmess.recorder import selection_cost

from . import theme

#: Past this many traces one chart stops being readable, whatever the colours.
SOFT_GRAPH_LIMIT = theme.MAX_SERIES_EXTENDED

PROFILE_FILTER = "Logging profiles (*.json);;All files (*)"


class ChannelPicker(QtWidgets.QWidget):
    """A searchable, page-grouped channel tree with a live cost estimate."""

    COL_NAME, COL_GRAPH, COL_ADDR, COL_UNIT, COL_ABOUT = range(5)

    #: Emitted (debounced) whenever a tick changes.
    selection_changed = QtCore.Signal()

    def __init__(
        self, channels: ChannelSet, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.channel_set = channels
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._by_name: dict[str, Channel] = {}
        #: The order names were given in. A profile's order is the CSV's column
        #: order and the panel's row order, so it is kept rather than re-sorted.
        self._order: list[str] = []
        self._measured_bytes_per_second = 0.0
        self._changing = False

        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self.selection_changed.emit)

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("filter by name, unit or description")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        row.addWidget(self.search, stretch=1)

        self.only_ticked = QtWidgets.QCheckBox("only ticked")
        self.only_ticked.setToolTip("Show just the channels that are being logged.")
        self.only_ticked.stateChanged.connect(lambda _=None: self._apply_filter())
        row.addWidget(self.only_ticked)

        starter = QtWidgets.QPushButton("Page-0 starter set")
        starter.setToolTip(
            "Ten page-0x00 channels: rpm, load, throttle, ignition, both\n"
            "temperatures, battery, target rpm, air mass, status.\n"
            "One wire byte each, no parity switches -- the fastest useful set."
        )
        starter.clicked.connect(self.select_starter_set)
        row.addWidget(starter)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(lambda: self.set_selection([], []))
        row.addWidget(clear)
        box.addLayout(row)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["log", "graph", "addr", "unit", "description"])
        self.tree.headerItem().setToolTip(
            self.COL_NAME, "Ticked: the channel is read from the ECU and written "
            "to the CSV."
        )
        self.tree.headerItem().setToolTip(
            self.COL_GRAPH, "Ticked: the channel is drawn on the chart. "
            "Unticked, it is still logged and still shows its value."
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setColumnWidth(self.COL_NAME, 190)
        self.tree.setColumnWidth(self.COL_GRAPH, 54)
        self.tree.setColumnWidth(self.COL_ADDR, 62)
        self.tree.setColumnWidth(self.COL_UNIT, 84)
        self.tree.itemChanged.connect(self._on_item_changed)
        box.addWidget(self.tree, stretch=1)

        self.cost_label = QtWidgets.QLabel()
        self.cost_label.setObjectName("cost")
        self.cost_label.setWordWrap(True)
        box.addWidget(self.cost_label)

        self._populate()
        self._update_cost()

    # -- building ----------------------------------------------------------

    def _add_item(
        self, group: QtWidgets.QTreeWidgetItem, channel: Channel, tip: list[str]
    ) -> None:
        item = QtWidgets.QTreeWidgetItem(
            [
                channel.name,
                "",
                f"0x{channel.addr:04X}" if channel.addr >= 0 else "computed",
                channel.unit_label or "-",
                channel.about,
            ]
        )
        item.setFlags(
            QtCore.Qt.ItemFlag.ItemIsEnabled
            | QtCore.Qt.ItemFlag.ItemIsSelectable
            | QtCore.Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(self.COL_NAME, QtCore.Qt.CheckState.Unchecked)
        item.setCheckState(self.COL_GRAPH, QtCore.Qt.CheckState.Unchecked)
        item.setData(self.COL_NAME, QtCore.Qt.ItemDataRole.UserRole, channel.name)
        for column in range(self.tree.columnCount()):
            item.setToolTip(column, "\n".join(tip))
        group.addChild(item)
        self._items[channel.name] = item
        self._by_name[channel.name] = channel

    def _populate(self) -> None:
        self.tree.blockSignals(True)

        # Computed columns first: they are the ones a tuner asks for by name and
        # the ones a page-by-page list would otherwise have nowhere to put.
        computed = [c for cls in DERIVATIONS.values() for c in cls().channels()]
        group = QtWidgets.QTreeWidgetItem(
            [f"computed  -  {len(computed)} columns  (free: no wire bytes of their own)"]
        )
        group.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        group.setForeground(0, QtCore.Qt.GlobalColor.gray)
        group.setFirstColumnSpanned(True)
        self.tree.addTopLevelItem(group)
        group.setFirstColumnSpanned(True)
        for channel in computed:
            needs = DERIVATIONS[DERIVED_COLUMNS[channel.name]].inputs
            self._add_item(group, channel, [
                channel.about,
                "computed from " + ", ".join(needs) + " -- read automatically "
                "when this is ticked",
            ])
        group.setExpanded(True)

        pages: dict[int, list[Channel]] = {}
        for channel in self.channel_set:
            pages.setdefault(channel.page, []).append(channel)
        for page in sorted(pages):
            members = sorted(pages[page], key=lambda c: c.addr)
            note = "  (cheapest: no latch byte)" if page == 0x00 else ""
            group = QtWidgets.QTreeWidgetItem(
                [f"page 0x{page:02X}  -  {len(members)} channels{note}"]
            )
            group.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            group.setForeground(0, QtCore.Qt.GlobalColor.gray)
            self.tree.addTopLevelItem(group)
            group.setFirstColumnSpanned(True)

            for channel in members:
                tip = [channel.about or channel.name]
                # The German stays visible underneath: it is the source text, and
                # anyone cross-checking against the FR or a DAMOS needs the original.
                if channel.description_en and channel.description:
                    tip.append(f"FR/DAMOS: {channel.description}")
                tip.append(f"address 0x{channel.addr:04X}, {channel.size} byte(s)")
                if channel.size == 2:
                    tip.append(f"word at 0x{channel.base:04X}..0x{channel.base + 1:04X}")
                tip.append(f"conversion {channel.umr or '(none)'}")
                if channel.suspect:
                    tip.append(f"REPORTED RAW: {channel.suspect}")
                elif not channel.has_physical:
                    tip.append(f"REPORTED RAW: no formula for {channel.umr}")
                self._add_item(group, channel, tip)
                if channel.suspect or not channel.has_physical:
                    self._items[channel.name].setForeground(
                        self.COL_NAME, QtCore.Qt.GlobalColor.darkYellow
                    )

        self.tree.expandItem(self.tree.topLevelItem(1))
        self.tree.blockSignals(False)

    # -- selection ---------------------------------------------------------

    def knows(self, name: str) -> bool:
        return name in self._items

    def _checked(self, name: str, column: int) -> bool:
        return self._items[name].checkState(column) == QtCore.Qt.CheckState.Checked

    def _ordered(self, column: int) -> list[str]:
        """Ticked names: the given order first, then anything ticked since."""
        ticked = [n for n in self._items if self._checked(n, column)]
        known = set(ticked)
        first = [n for n in self._order if n in known]
        seen = set(first)
        return first + [n for n in ticked if n not in seen]

    def log_names(self) -> list[str]:
        return self._ordered(self.COL_NAME)

    def graph_names(self) -> list[str]:
        return self._ordered(self.COL_GRAPH)

    def ecu_channels(self) -> list[Channel]:
        """What is actually read: the ticked channels plus computed columns' inputs."""
        channels, _ = expand_selection(self.channel_set, self.log_names())
        return [c for c in channels if not c.is_virtual]

    def implied_inputs(self) -> list[str]:
        """Channels read only because a computed column needs them."""
        asked = set(self.log_names())
        return [c.name for c in self.ecu_channels() if c.name not in asked]

    def set_selection(self, log: list[str], graph: list[str] | None = None) -> None:
        """Tick these. ``graph=None`` draws every quantity among them."""
        self._order = [n for n in log if n in self._items]
        wanted = set(self._order)
        if graph is None:
            drawn = {
                n for n in wanted
                if belongs_on_graph(n, self._by_name[n].unit_label)
            }
        else:
            drawn = set(graph) & wanted
        self._changing = True
        self.tree.blockSignals(True)
        for name, item in self._items.items():
            item.setCheckState(
                self.COL_NAME,
                QtCore.Qt.CheckState.Checked if name in wanted
                else QtCore.Qt.CheckState.Unchecked,
            )
            item.setCheckState(
                self.COL_GRAPH,
                QtCore.Qt.CheckState.Checked if name in drawn
                else QtCore.Qt.CheckState.Unchecked,
            )
        self.tree.blockSignals(False)
        self._changing = False
        self._update_cost()
        self._apply_filter()
        self.selection_changed.emit()

    def select_starter_set(self) -> None:
        from mcmess.channels import PAGE0_STARTER

        self.set_selection([n for n in PAGE0_STARTER if n in self._items])

    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if self._changing or column not in (self.COL_NAME, self.COL_GRAPH):
            return
        name = item.data(self.COL_NAME, QtCore.Qt.ItemDataRole.UserRole)
        if name is None:
            return
        self._changing = True
        logged = item.checkState(self.COL_NAME) == QtCore.Qt.CheckState.Checked
        if column == self.COL_NAME:
            # Newly logged: draw it if it is a quantity. No longer logged: it
            # cannot be drawn either, there is nothing to draw.
            draw = logged and belongs_on_graph(name, self._by_name[name].unit_label)
            item.setCheckState(
                self.COL_GRAPH,
                QtCore.Qt.CheckState.Checked if draw
                else QtCore.Qt.CheckState.Unchecked,
            )
        elif item.checkState(self.COL_GRAPH) == QtCore.Qt.CheckState.Checked and not logged:
            # Asking for a trace is asking for the channel.
            item.setCheckState(self.COL_NAME, QtCore.Qt.CheckState.Checked)
        self._changing = False
        self._update_cost()
        self._debounce.start()

    # -- cost --------------------------------------------------------------

    def set_measured_rate(self, bytes_per_second: float) -> None:
        """Feed the live byte rate in so the Hz estimate stops being a guess."""
        if bytes_per_second > 0:
            self._measured_bytes_per_second = bytes_per_second
            self._update_cost()

    def _update_cost(self) -> None:
        logged = self.log_names()
        if not logged:
            self.cost_label.setObjectName("cost")
            self.cost_label.setText("No channels ticked.")
            self._restyle(self.cost_label)
            return

        channels = self.ecu_channels()
        cost = selection_cost(channels)
        graphed = len(self.graph_names())
        measured = self._measured_bytes_per_second
        if measured:
            rate = (f"~{cost.estimated_hz(measured):.1f} Hz at the measured "
                    f"{measured:.0f} bytes/s")
        else:
            rate = (f"~{cost.estimated_hz(240.0):.0f}-{cost.estimated_hz(480.0):.0f} "
                    "Hz once connected (estimated)")

        lines = [
            f"{len(logged)} logged, {graphed} on the graph  ·  "
            f"{cost.byte_count} bytes read, {cost.wire_bytes} wire bytes/sample, "
            f"{cost.parity_switches} parity switches, {cost.pages} pages  ·  {rate}"
        ]
        implied = self.implied_inputs()
        if implied:
            lines.append(
                "Also read, because a computed column needs them: "
                + ", ".join(implied)
            )
        warn = graphed > SOFT_GRAPH_LIMIT
        if warn:
            lines.append(
                f"More than {SOFT_GRAPH_LIMIT} traces on one chart: the extra ones "
                "draw in neutral ink rather than a recycled colour."
            )
        self.cost_label.setObjectName("costWarn" if warn else "cost")
        self.cost_label.setText("\n".join(lines))
        self._restyle(self.cost_label)

    @staticmethod
    def _restyle(widget: QtWidgets.QWidget) -> None:
        """Re-apply the stylesheet after an objectName change."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # -- filtering ---------------------------------------------------------

    def _apply_filter(self, _text: str | None = None) -> None:
        needle = self.search.text().strip().lower()
        only = self.only_ticked.isChecked()
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            visible_children = 0
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                name = child.data(self.COL_NAME, QtCore.Qt.ItemDataRole.UserRole)
                channel = self._by_name[name]
                haystack = " ".join(
                    (
                        channel.name,
                        channel.unit,
                        channel.description,
                        channel.description_en,
                        channel.umr,
                        f"{channel.addr:04x}" if channel.addr >= 0 else "computed",
                    )
                ).lower()
                match = (not needle or needle in haystack) and (
                    not only or self._checked(name, self.COL_NAME)
                )
                child.setHidden(not match)
                visible_children += int(match)
            group.setHidden(visible_children == 0)
            if (needle or only) and visible_children:
                group.setExpanded(True)


class ChannelDialog(QtWidgets.QDialog):
    """The window that comes before connecting: what to log, what to draw."""

    def __init__(
        self,
        channels: ChannelSet,
        profile: Profile,
        parent: QtWidgets.QWidget | None = None,
        *,
        connecting: bool = True,
        measured_bytes_per_second: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self.channel_set = channels
        self._path: Path | None = profile.path
        self._ecu = profile.ecu or (channels.part_number or "")
        self._saved: tuple[list[str], list[str]] | None = None
        self.setWindowTitle("Channels to log")
        self.resize(980, 760)

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.profile_label = QtWidgets.QLabel()
        top.addWidget(self.profile_label, stretch=1)
        load = QtWidgets.QPushButton("Load profile...")
        load.clicked.connect(self._load)
        top.addWidget(load)
        self.save_button = QtWidgets.QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        top.addWidget(self.save_button)
        save_as = QtWidgets.QPushButton("Save as...")
        save_as.clicked.connect(self._save_as)
        top.addWidget(save_as)
        box.addLayout(top)

        hint = QtWidgets.QLabel(
            "<b>log</b> writes the channel to the CSV and shows its value.  "
            "<b>graph</b> also draws it. Log everything you might want afterwards; "
            "draw only what you want to watch."
        )
        hint.setObjectName("caveat")
        hint.setWordWrap(True)
        box.addWidget(hint)

        self.picker = ChannelPicker(channels)
        self.picker.set_measured_rate(measured_bytes_per_second)
        self.picker.selection_changed.connect(self._refresh_title)
        box.addWidget(self.picker, stretch=1)

        buttons = QtWidgets.QDialogButtonBox()
        self.ok_button = buttons.addButton(
            "Connect" if connecting else "OK",
            QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.ok_button.setObjectName("primary")
        self.ok_button.setDefault(True)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

        self._apply(profile)

    # -- the profile -------------------------------------------------------

    def _apply(self, profile: Profile) -> None:
        unknown = [n for n in profile.log if not self.picker.knows(n)]
        self._path = profile.path
        self.picker.set_selection(profile.log, profile.graph)
        self._saved = (
            (self.picker.log_names(), self.picker.graph_names())
            if profile.path else None
        )
        self._refresh_title()
        if unknown:
            self._warn(
                "Unknown channels",
                f"{profile.name} names channels this ECU's database does not "
                f"have, so they were left out: {', '.join(unknown)}",
            )

    def profile(self) -> Profile:
        """What is ticked now, as a profile."""
        return Profile(
            log=self.picker.log_names(),
            graph=self.picker.graph_names(),
            ecu=self._ecu,
            path=self._path,
        )

    @property
    def modified(self) -> bool:
        return self._saved != (self.picker.log_names(), self.picker.graph_names())

    def _refresh_title(self) -> None:
        name = self._path.name if self._path else "not saved to a file"
        changed = "  (changed since it was saved)" if self._path and self.modified else ""
        self.profile_label.setText(
            f'<span style="color:{theme.TEXT_MUTED}">Profile</span>  '
            f"<b>{name}</b>"
            f'<span style="color:{theme.STATUS_WARNING}">{changed}</span>'
        )
        self.save_button.setEnabled(bool(self._path) and self.modified)
        self.ok_button.setEnabled(bool(self.picker.log_names()))

    def _load(self) -> None:
        start = str(self._path.parent if self._path else PROFILE_DIR)
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load a logging profile", start, PROFILE_FILTER
        )
        if chosen:
            self.load_path(Path(chosen))

    def load_path(self, path: Path) -> bool:
        try:
            profile = load_profile(path)
        except ProfileError as exc:
            self._warn("Cannot load the profile", str(exc))
            return False
        if profile.ecu and self.channel_set.part_number and (
            profile.ecu != self.channel_set.part_number
        ):
            self._warn(
                "A profile for another ECU",
                f"{path.name} was saved for {profile.ecu}; this is "
                f"{self.channel_set.part_number}. Channel names it shares are "
                "ticked, but check the list before trusting it.",
            )
        self._apply(profile)
        return True

    def _save(self) -> None:
        if self._path is None:
            self._save_as()
        else:
            self.save_path(self._path)

    def _save_as(self) -> None:
        start = str(self._path or (PROFILE_DIR / "profile.json"))
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save the logging profile", start, PROFILE_FILTER
        )
        if chosen:
            path = Path(chosen)
            self.save_path(path if path.suffix else path.with_suffix(".json"))

    def save_path(self, path: Path) -> bool:
        profile = self.profile()
        try:
            save_profile(profile, path)
        except OSError as exc:
            self._warn("Cannot save the profile", f"{path.name}: {exc.strerror or exc}")
            return False
        self._path = path
        self._saved = (self.picker.log_names(), self.picker.graph_names())
        self._refresh_title()
        return True

    def _accept(self) -> None:
        if not self.picker.ecu_channels():
            self._warn(
                "No channels",
                "Tick at least one channel to log before connecting.",
            )
            return
        self.accept()

    def _warn(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, message)
