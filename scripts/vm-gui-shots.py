#!/usr/bin/env python3
"""Guest-side screenshots of every page of the installer, in Guided and in Advanced mode (hardware backlog: screenshots).

Run by scripts/test-vm.py as the live user inside the live session of a VM (``--screenshots``), once per screen size. It opens the real
installer window, shows each page of ``window.pages`` in turn (the pages are discovered, so a page added later is photographed too) with
the mode switch in each position, waits for the page to settle and saves ``window.grab()`` as a PNG. It never starts an installation, never
presses Continue and never touches a disk: pages are shown with ``_show_page`` on a default manifest.

    vm-gui-shots.py OUTDIR [--socket SOCKET] [--settle MS] [--modes guided,advanced]

OUTDIR gets ``page-NN-<page>-<mode>.png`` and ``shots.json``: the screen and window sizes, whether the window fits the screen, and per page whether its
content fits (no horizontal scroll bar needed, nothing cut off at the bottom of a scroll area). The last line printed is that JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

MODES = ("guided", "advanced")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "page"


def page_name(page: Any, index: int) -> str:
    return slug(getattr(page, "objectName", lambda: "")() or type(page).__name__.removesuffix("Page")) or f"page{index}"


def fit_problems(window_size: tuple[int, int], screen_size: tuple[int, int]) -> list[str]:
    """The installer window must fit the screen it is shown on (1366x768 is a real laptop size)."""
    problems = []
    if window_size[0] > screen_size[0]:
        problems.append(f"the window is {window_size[0]} px wide on a {screen_size[0]} px wide screen")
    if window_size[1] > screen_size[1]:
        problems.append(f"the window is {window_size[1]} px high on a {screen_size[1]} px high screen")
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--socket", default="/run/gentoo-installer/server.sock", help="the installer daemon's socket (the readiness page shows what it says)")
    parser.add_argument("--settle", type=int, default=600, help="milliseconds to wait after showing a page before the picture (default 600)")
    parser.add_argument("--modes", default=",".join(MODES), help="guided, advanced or both (default)")
    parser.add_argument("--scan-wait", type=int, default=20, help="seconds to wait for the live hardware scan before taking the pictures (default 20)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print(json.dumps({"type": "error", "error": "run the screenshots as the live user, not as root"}))
        return 2
    modes = [mode for mode in args.modes.split(",") if mode]
    if not modes or any(mode not in MODES for mode in modes):
        print(json.dumps({"type": "error", "error": f"--modes takes guided and/or advanced, not {args.modes!r}"}))
        return 2
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QScrollArea

    from gentoo_installer.app import InstallerWindow
    from gentoo_installer.manifest import InstallManifest
    from gentoo_installer.theme import apply_theme

    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(["gentoo-installer-shots"])
    apply_theme(app)

    def wait(milliseconds: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    window = InstallerWindow(InstallManifest(), args.socket, firmware_explicit=False)
    window.show()
    wait(500)
    for _ in range(max(1, args.scan_wait * 4)):  # the readiness page fills in when the daemon has scanned the machine
        if getattr(window.info, "inspected", True):
            break
        wait(250)
    screen = window.screen()
    screen_size = (screen.size().width(), screen.size().height()) if screen else (0, 0)
    available = screen.availableGeometry() if screen else None
    report: dict[str, Any] = {"type": "gui_shots", "screen": list(screen_size), "available": [available.width(), available.height()] if available else [],
                              "pages": [], "problems": []}
    for mode in modes:
        index_of_mode = window.mode.findData(mode)
        if index_of_mode >= 0:
            window.mode.setCurrentIndex(index_of_mode)
        wait(args.settle)
        for index, page in enumerate(window.pages):
            window._show_page(index)
            wait(args.settle)
            name = f"page-{index:02d}-{page_name(page, index)}-{mode}.png"
            image = window.grab()
            if not image.save(str(args.output / name)):
                report["problems"].append(f"{name}: the picture could not be saved")
                continue
            scrolled = [area for area in page.findChildren(QAbstractScrollArea) if area.isVisibleTo(page)]
            tall = next((area for area in page.findChildren(QScrollArea) if area.isVisibleTo(page) and area.widget() is not None
                         and area.verticalScrollBar().maximum() > 0), None)
            if tall is not None:  # what a scroll bar hides: the whole content of the page, in one tall picture
                full = name.replace(".png", "-full.png")
                content = tall.widget()
                if content is not None and content.grab().save(str(args.output / full)):
                    report["pages"].append({"file": full, "index": index, "page": page_name(page, index), "mode": mode, "full_page_of": name})
            horizontal = [type(area).__name__ for area in scrolled if area.horizontalScrollBar().isVisibleTo(area) and area.horizontalScrollBar().maximum() > 0]
            report["pages"].append({"file": name, "index": index, "page": page_name(page, index), "mode": mode, "title": window.windowTitle(),
                                    "size": [image.width(), image.height()], "horizontal_scroll": horizontal})
            if horizontal:
                report["problems"].append(f"{name}: needs horizontal scrolling ({', '.join(horizontal)})")
    frame = window.frameGeometry()
    report["window"] = [frame.width(), frame.height()]
    report["problems"] += fit_problems((frame.width(), frame.height()), report["available"] and tuple(report["available"]) or screen_size)
    (args.output / "shots.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    window.close()
    return 1 if not report["pages"] else 0


if __name__ == "__main__":
    sys.exit(main())
