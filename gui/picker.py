"""Channel picker.

244 channels is too many to scroll blindly, and they are not equal: a page-0x00
channel costs one wire byte, while the first channel on any other page also costs
a latch byte and two parity switches. Since throughput is the binding constraint,
the picker shows what a selection costs and what sample rate it implies, updating
as you tick boxes.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from mcmess.channels import Channel, ChannelSet
from mcmess.recorder import selection_cost

from . import theme

#: Beyond this many channels the plot stack stops being readable and the hue order
#: is exhausted, so extra panels draw in neutral ink.
SOFT_PANEL_LIMIT = theme.MAX_SERIES


class ChannelPicker(QtWidgets.QWidget):
    """A searchable, page-grouped channel tree with a live cost estimate."""

    #: Emitted (debounced) with the new selection.
    selection_changed = QtCore.Signal(list)

    def __init__(
        self, channels: ChannelSet, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.channel_set = channels
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._measured_bytes_per_second = 0.0

        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._emit_selection)

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)

        heading = QtWidgets.QLabel("Channels")
        heading.setObjectName("heading")
        box.addWidget(heading)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("filter by name, unit or description")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        box.addWidget(self.search)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(4)
        starter = QtWidgets.QPushButton("Page-0 starter set")
        starter.setToolTip(
            "Ten page-0x00 channels: rpm, load, throttle, ignition, both\n"
            "temperatures, battery, target rpm, air mass, status.\n"
            "One wire byte each, no parity switches -- the fastest useful set."
        )
        starter.clicked.connect(self.select_starter_set)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear_selection)
        buttons.addWidget(starter)
        buttons.addWidget(clear)
        box.addLayout(buttons)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["channel", "addr", "unit"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setColumnWidth(0, 190)
        self.tree.setColumnWidth(1, 56)
        self.tree.itemChanged.connect(self._on_item_changed)
        box.addWidget(self.tree, stretch=1)

        self.cost_label = QtWidgets.QLabel()
        self.cost_label.setObjectName("cost")
        self.cost_label.setWordWrap(True)
        box.addWidget(self.cost_label)

        self._populate()
        self._update_cost()

    # -- building ----------------------------------------------------------

    def _populate(self) -> None:
        pages: dict[int, list[Channel]] = {}
        for channel in self.channel_set:
            pages.setdefault(channel.page, []).append(channel)

        self.tree.blockSignals(True)
        for page in sorted(pages):
            members = sorted(pages[page], key=lambda c: c.addr)
            note = "  (cheapest: no latch byte)" if page == 0x00 else ""
            group = QtWidgets.QTreeWidgetItem(
                [f"page 0x{page:02X}  -  {len(members)} channels{note}", "", ""]
            )
            group.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            group.setForeground(0, QtCore.Qt.GlobalColor.gray)
            self.tree.addTopLevelItem(group)

            for channel in members:
                item = QtWidgets.QTreeWidgetItem(
                    [
                        channel.name,
                        f"0x{channel.addr:04X}",
                        channel.unit_label or "-",
                    ]
                )
                item.setFlags(
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, channel.name)

                tip = [channel.description or channel.name]
                tip.append(f"address 0x{channel.addr:04X}, {channel.size} byte(s)")
                if channel.size == 2:
                    tip.append(f"word at 0x{channel.base:04X}..0x{channel.base + 1:04X}")
                tip.append(f"conversion {channel.umr or '(none)'}")
                if channel.suspect:
                    tip.append(f"REPORTED RAW: {channel.suspect}")
                    item.setForeground(0, QtCore.Qt.GlobalColor.darkYellow)
                elif not channel.has_physical:
                    tip.append(f"REPORTED RAW: no formula for {channel.umr}")
                    item.setForeground(0, QtCore.Qt.GlobalColor.darkYellow)
                item.setToolTip(0, "\n".join(tip))

                group.addChild(item)
                self._items[channel.name] = item

        self.tree.expandItem(self.tree.topLevelItem(0))
        self.tree.blockSignals(False)

    # -- selection ---------------------------------------------------------

    def selected_channels(self) -> list[Channel]:
        """Checked channels, ordered as the tree shows them (page, then address)."""
        out = []
        for name, item in self._items.items():
            if item.checkState(0) == QtCore.Qt.CheckState.Checked:
                out.append(self.channel_set.get(name))
        return sorted(out, key=lambda c: (c.page, c.addr))

    def set_selection(self, names: list[str]) -> None:
        self.tree.blockSignals(True)
        wanted = set(names)
        for name, item in self._items.items():
            item.setCheckState(
                0,
                QtCore.Qt.CheckState.Checked
                if name in wanted
                else QtCore.Qt.CheckState.Unchecked,
            )
        self.tree.blockSignals(False)
        self._update_cost()
        self._emit_selection()

    def select_starter_set(self) -> None:
        from mcmess.channels import PAGE0_STARTER

        self.set_selection([n for n in PAGE0_STARTER if n in self._items])

    def clear_selection(self) -> None:
        self.set_selection([])

    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        self._update_cost()
        self._debounce.start()

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.selected_channels())

    # -- cost --------------------------------------------------------------

    def set_measured_rate(self, bytes_per_second: float) -> None:
        """Feed the live byte rate in so the Hz estimate stops being a guess."""
        if bytes_per_second > 0:
            self._measured_bytes_per_second = bytes_per_second
            self._update_cost()

    def _update_cost(self) -> None:
        channels = self.selected_channels()
        cost = selection_cost(channels)
        if not channels:
            self.cost_label.setObjectName("cost")
            self.cost_label.setText("No channels selected.")
            self._restyle(self.cost_label)
            return

        measured = self._measured_bytes_per_second
        rate_text = ""
        if measured:
            rate_text = f"\n~{cost.estimated_hz(measured):.1f} Hz at the measured {measured:.0f} bytes/s"
        else:
            low = cost.estimated_hz(240.0)
            high = cost.estimated_hz(480.0)
            rate_text = f"\n~{low:.0f}-{high:.0f} Hz once connected (estimated)"

        lines = [
            f"{cost.channel_count} channels, {cost.byte_count} bytes "
            f"({cost.page0_channels} in page 0x00)",
            f"{cost.wire_bytes} wire bytes/sample, "
            f"{cost.parity_switches} parity switches, {cost.pages} pages",
        ]
        warn = cost.channel_count > SOFT_PANEL_LIMIT
        if warn:
            lines.append(
                f"Over {SOFT_PANEL_LIMIT} channels: extra panels draw in neutral "
                "ink rather than a recycled colour."
            )
        self.cost_label.setObjectName("costWarn" if warn else "cost")
        self.cost_label.setText("\n".join(lines) + rate_text)
        self._restyle(self.cost_label)

    @staticmethod
    def _restyle(widget: QtWidgets.QWidget) -> None:
        """Re-apply the stylesheet after an objectName change."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # -- filtering ---------------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            visible_children = 0
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                name = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
                channel = self.channel_set.get(name)
                haystack = " ".join(
                    (
                        channel.name,
                        channel.unit,
                        channel.description,
                        channel.umr,
                        f"{channel.addr:04x}",
                    )
                ).lower()
                match = not needle or needle in haystack
                child.setHidden(not match)
                visible_children += int(match)
            group.setHidden(visible_children == 0)
            if needle and visible_children:
                group.setExpanded(True)
