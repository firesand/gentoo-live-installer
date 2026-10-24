#!/usr/bin/env python3
"""HWMonux - an HWMonitor-style hardware sensor viewer for Linux."""

import argparse
import logging
import signal
import sys
import threading
import time

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog, QHeaderView, QMainWindow, QMessageBox, QTreeWidget, QTreeWidgetItem

from sensors import Monitor, format_value, report

# Device icon key -> freedesktop icon names, first one the theme has wins.
ICONS = {
    "board": ("motherboard", "computer"),
    "cpu": ("cpu", "preferences-devices-cpu"),
    "cooler": ("fan", "temperature-normal"),
    "memory": ("memory", "media-flash"),
    "gpu": ("video-card", "video-display"),
    "drive": ("drive-harddisk",),
    "network": ("network-wireless", "network-card"),
    "chip": ("audio-card", "cpu"),
}
INTERVALS_MS = (500, 1000, 2000, 5000)
WARN_COLOR, CRIT_COLOR = QColor("#f67400"), QColor("#da4453")
NUMERIC = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
WINDOW_SIZE = (800, 900)
# Largest share of the usable screen the window may start with: laptop panels (1366x768,
# 1280x720) are smaller than the default size, and a window manager that does not shrink
# oversized windows would put its title bar or the bottom edge out of reach.
SCREEN_SHARE = 0.9
POLLER_JOIN_SECONDS = 1.0

log = logging.getLogger("hwmonux")


def clamp_size(width, height, available_width, available_height, share=SCREEN_SHARE):
    """The requested window size, shrunk to at most `share` of the available screen area."""
    return min(width, int(available_width * share)), min(height, int(available_height * share))


def themed_icon(key):
    for name in ICONS[key]:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon()


class MainWindow(QMainWindow):
    polled = Signal()

    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor
        self.settings = QSettings("HWMonux", "HWMonux")
        self.fahrenheit = self.settings.value("fahrenheit", False, bool)
        self.setWindowTitle("HWMonux")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Sensor", "Value", "Min", "Max"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.setCentralWidget(self.tree)
        self._rows = []  # (tree item, Sensor)
        self._populate()

        self._poller = None
        self._closing = False
        self.polled.connect(self._show_values)
        self.timer = QTimer(self, timeout=self.poll)
        self._build_menus()
        self._show_values()
        self._size_columns()
        self.resize(*WINDOW_SIZE)
        self.restoreGeometry(self.settings.value("geometry", b""))
        self._fit_to_screen()
        self.timer.start()

    def _fit_to_screen(self):
        # self.screen() can still be None before the window is shown (offscreen platform).
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = clamp_size(self.width(), self.height(), area.width(), area.height())
        if size != (self.width(), self.height()):
            self.resize(*size)

    def _populate(self):
        root = QTreeWidgetItem(self.tree, [self.monitor.hostname])
        root.setIcon(0, QIcon.fromTheme("computer"))
        for dev in self.monitor.devices:
            node = QTreeWidgetItem(root, [dev.name])
            node.setFirstColumnSpanned(True)  # long device names shouldn't be cut at the first column
            node.setIcon(0, themed_icon(dev.icon))
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            if dev.info:
                info = QTreeWidgetItem(node, ["Information"])
                for label, text in dev.info:
                    QTreeWidgetItem(info, [label, text]).setToolTip(1, text)
            for group, sensors in dev.groups.items():
                group_node = QTreeWidgetItem(node, [group])
                core_nodes = {}
                for sensor in sensors:
                    parent = group_node
                    if dev.icon == "cpu" and group in ("Temperatures", "Clocks", "Utilization"):
                        # The backend names cores from the kernel's P/E topology.
                        # Package totals and unclassified cores stay directly in the category.
                        for prefix, title in (("P-Core #", "P-Cores"), ("E-Core #", "E-Cores")):
                            if sensor.label.startswith(prefix):
                                if title not in core_nodes:
                                    core_nodes[title] = QTreeWidgetItem(group_node, [title])
                                parent = core_nodes[title]
                                break
                    item = QTreeWidgetItem(parent, [sensor.label])
                    for column in (1, 2, 3):
                        item.setTextAlignment(column, NUMERIC)
                    self._rows.append((item, sensor))
        self.tree.expandAll()

    def _size_columns(self):
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        numeric = self.fontMetrics().horizontalAdvance("0000.0 MiB/s") + 24
        # The value column also holds the Information texts, which are much longer.
        self.tree.setColumnWidth(1, max(numeric, self.tree.sizeHintForColumn(1) + 12))
        self.tree.setColumnWidth(2, numeric)
        self.tree.setColumnWidth(3, numeric)
        for column in (1, 2, 3):
            self.tree.headerItem().setTextAlignment(column, NUMERIC)

    def _build_menus(self):
        def action(menu, text, slot, shortcut=None, icon=None):
            act = QAction(QIcon.fromTheme(icon) if icon else QIcon(), text, self)
            if shortcut:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        file_menu = self.menuBar().addMenu("&File")
        action(file_menu, "&Save Report…", self.save_report, QKeySequence.StandardKey.Save, "document-save")
        file_menu.addSeparator()
        action(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit, "application-exit")

        view_menu = self.menuBar().addMenu("&View")
        action(view_menu, "&Reset Min/Max", self.reset_min_max, "Ctrl+R", "edit-clear-history")
        view_menu.addSeparator()
        action(view_menu, "&Expand All", self.tree.expandAll)
        action(view_menu, "&Collapse All", self._collapse_devices)
        view_menu.addSeparator()
        fahrenheit = action(view_menu, "Temperatures in &Fahrenheit", self.set_fahrenheit)
        fahrenheit.setCheckable(True)
        fahrenheit.setChecked(self.fahrenheit)

        interval_menu = view_menu.addMenu("Update &Interval")
        group = QActionGroup(self)
        current = self.settings.value("interval", 1000, int)
        for ms in INTERVALS_MS:
            act = action(interval_menu, f"{ms / 1000:g} s", lambda _, ms=ms: self.set_interval(ms))
            act.setCheckable(True)
            act.setChecked(ms == current)
            group.addAction(act)
        self.timer.setInterval(current)

    def _collapse_devices(self):
        self.tree.collapseAll()
        self.tree.expandToDepth(0)  # keep the device list itself visible

    def poll(self):
        # NVMe and SMBus sensor reads are device commands taking milliseconds each, so they
        # run off the GUI thread. A poll that outlasts the interval just skips ticks.
        if self._poller is None or not self._poller.is_alive():
            self._poller = threading.Thread(target=self._poll_worker, daemon=True)
            self._poller.start()

    def _poll_worker(self):
        try:
            self.monitor.poll()
        except Exception:
            log.warning("polling the sensors failed", exc_info=True)
        finally:
            # Always report back, so one failing poll never leaves the values frozen.
            if not self._closing:
                try:
                    self.polled.emit()
                except RuntimeError:
                    pass  # the window was destroyed while this poll was still running

    def _show_values(self):
        for item, sensor in self._rows:
            for column, value in enumerate((sensor.value, sensor.min, sensor.max), 1):
                item.setText(column, format_value(sensor.kind, value, self.fahrenheit))
            brush = QBrush()
            if sensor.value is not None:
                if sensor.crit is not None and sensor.value >= sensor.crit:
                    brush = QBrush(CRIT_COLOR)
                elif sensor.warn is not None and sensor.value >= sensor.warn:
                    brush = QBrush(WARN_COLOR)
            item.setForeground(1, brush)

    def reset_min_max(self):
        self.monitor.reset()
        self._show_values()

    def set_fahrenheit(self, enabled):
        self.fahrenheit = enabled
        self.settings.setValue("fahrenheit", enabled)
        self._show_values()

    def set_interval(self, ms):
        self.timer.setInterval(ms)
        self.settings.setValue("interval", ms)

    def save_report(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Report", f"hwmonux-{self.monitor.hostname}.txt", "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(report(self.monitor, self.fahrenheit))
        except OSError as e:
            QMessageBox.warning(self, "Save Report", f"Could not write {path}:\n{e.strerror}")

    def closeEvent(self, event):
        self._closing = True
        self.timer.stop()
        if self._poller:
            # A stuck sensor read (NVML, USB, a hung drive) must not keep the window open:
            # the poller is a daemon thread and dies with the process.
            self._poller.join(timeout=POLLER_JOIN_SECONDS)
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", action="store_true",
                        help="print one snapshot of all sensors to stdout instead of opening the window")
    args, qt_args = parser.parse_known_args()

    monitor = Monitor()
    if args.dump:
        time.sleep(1)  # utilization, power and disk rates need two samples
        monitor.poll()
        print(report(monitor), end="")
        return 0

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C in the terminal quit
    app = QApplication(sys.argv[:1] + qt_args)
    app.setApplicationName("HWMonux")
    app.setDesktopFileName("hwmonux")
    app.setWindowIcon(QIcon.fromTheme("utilities-system-monitor"))
    window = MainWindow(monitor)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
