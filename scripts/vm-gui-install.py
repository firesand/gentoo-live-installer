#!/usr/bin/env python3
"""Guest-only GUI acceptance driver; uses the real client and root daemon.

Run as the live gentoo user inside the disposable QEMU VM. No installer mocks
or privileged subprocesses are used. The wizard is traversed through its real
Continue buttons; only the final review confirmation is automated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/gentoo-vm-gui"))
    parser.add_argument("--cancel-at-stage3", action="store_true",
                        help="Click the real GUI cancel button when the backend starts stage3")
    args = parser.parse_args()
    serial = Path("/sys/class/block/vda/serial")
    if not serial.exists():
        serial = Path("/sys/class/block/vda/device/serial")
    if os.geteuid() == 0 or not serial.is_file() or serial.read_text().strip() != "GENTOO-VM-DISPOSABLE":
        raise SystemExit("Refusing GUI automation outside the non-root disposable VM session")
    display = os.environ.get("DISPLAY", "")
    authority = Path(os.environ.get("XAUTHORITY", ""))
    if not re.fullmatch(r"(?::[0-9]+(?:\.[0-9]+)?|unix:[0-9]+(?:\.[0-9]+)?)", display):
        raise SystemExit("Refusing GUI automation without a local X11 DISPLAY")
    if not authority.is_absolute() or not authority.is_file() or not os.access(authority, os.R_OK):
        raise SystemExit("Refusing GUI automation without a readable XAUTHORITY file")
    from PySide6.QtCore import QObject, QTimer, Slot
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gentoo_installer.app import InstallerWindow
    from gentoo_installer.backend import BackendWorker
    from gentoo_installer.manifest import InstallManifest
    from gentoo_installer.theme import apply_theme

    manifest = InstallManifest.load(args.manifest)
    if manifest.storage.device != "/dev/vda" or manifest.storage.device_id != "GENTOO-VM-DISPOSABLE":
        raise SystemExit("Manifest does not select the disposable VM disk")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    app = QApplication(["gentoo-installer-vm-test"])
    apply_theme(app)
    expected_manifest = manifest.to_dict(redact=False)

    def save_screenshot(path: Path) -> None:
        if not window.grab().save(str(path)) or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Could not save nonempty screenshot: {path}")

    original_worker_init = BackendWorker.__init__
    cancel_state = {"requested": False, "storage_completed": False, "stage3_started": 0}

    class EventObserver(QObject):
        @Slot(dict)
        def on_event(self, event: dict) -> None:
            print(json.dumps(event), flush=True)
            if event.get("type") != "stage":
                return
            data = event.get("data", {})
            if event.get("stage") == "storage" and data.get("status") == "completed":
                cancel_state["storage_completed"] = True
            if event.get("stage") == "stage3" and data.get("status") == "running":
                cancel_state["stage3_started"] += 1
                if args.cancel_at_stage3 and not cancel_state["requested"]:
                    if not cancel_state["storage_completed"]:
                        print(json.dumps({"type": "error", "error": "stage3 started before storage completion"}), flush=True)
                        return
                    cancel_state["requested"] = True
                    window.progress.cancel.click()

    observer = EventObserver()

    def observed_worker_init(worker, *worker_args, **worker_kwargs):
        original_worker_init(worker, *worker_args, **worker_kwargs)
        worker.eventReceived.connect(observer.on_event)

    BackendWorker.__init__ = observed_worker_init
    window = InstallerWindow(manifest, "/run/gentoo-installer/server.sock")
    window.show()
    outcome: list[bool] = []

    def finish(success: bool) -> None:
        outcome.append(success)
        print(json.dumps({"type": "gui_result", "success": success,
                          "stage": window.progress.stage.text(),
                          "cancel_requested": cancel_state["requested"],
                          "storage_completed": cancel_state["storage_completed"],
                          "stage3_started": cancel_state["stage3_started"],
                          "reboot_enabled": not window.progress.reboot.isHidden()}), flush=True)
        QTimer.singleShot(100, save_and_exit)

    def save_and_exit() -> None:
        worker = window.progress.worker
        if worker is not None and worker.isRunning():
            QTimer.singleShot(100, save_and_exit)
            return
        try:
            save_screenshot(args.output / "result.png")
            (args.output / "gui.log").write_text(window.progress.log.toPlainText())
            if not (args.output / "gui.log").is_file() or (args.output / "gui.log").stat().st_size == 0:
                raise RuntimeError("Could not save nonempty GUI log")
        except OSError as exc:
            print(json.dumps({"type": "error", "error": f"Could not save GUI artifacts: {exc}"}), flush=True)
            app.exit(1)
            return
        except RuntimeError as exc:
            print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
            app.exit(1)
            return
        expected = (outcome == [False] and cancel_state["requested"] and
                    cancel_state["storage_completed"] and cancel_state["stage3_started"] == 1)
        app.exit(0 if (expected if args.cancel_at_stage3 else outcome == [True]) else 1)

    def confirm() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            yes = dialog.button(QMessageBox.StandardButton.Yes)
            if yes is not None and dialog.windowTitle() == "Erase the selected disk?":
                yes.click()
            else:
                print(json.dumps({"type": "error", "error": dialog.text()}), flush=True)
                app.exit(1)

    def fail_modal() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            print(json.dumps({"type": "error", "error": dialog.text(),
                              "dialog": dialog.windowTitle()}), flush=True)
            dialog.reject()
            app.exit(1)

    def verify_live_readiness() -> None:
        disk = next((item for item in window.info.disks if item.path == "/dev/vda"), None)
        identity = (getattr(disk, "wwid", "") or getattr(disk, "serial", "")) if disk else ""
        if (disk is None or identity != "GENTOO-VM-DISPOSABLE" or
                getattr(disk, "read_only", False) or getattr(disk, "mounted", False)):
            raise RuntimeError("Live scan did not identify an available disposable /dev/vda")
        if window.info.architecture not in {"x86_64", "amd64"}:
            raise RuntimeError(f"Unsupported live architecture: {window.info.architecture}")
        if not window.info.network or window.info.missing_tools:
            raise RuntimeError("Live readiness scan reports network failure or missing tools")
        if bool(window.info.uefi) != (manifest.storage.firmware == "uefi"):
            raise RuntimeError("Manifest firmware choice does not match the live VM firmware")

    navigation_errors: list[str] = []

    def start() -> None:
        try:
            # inspect_system() is synchronous in InstallerWindow.__init__, so
            # reaching this timer means the live disk/readiness scan is done.
            verify_live_readiness()
        except RuntimeError as exc:
            navigation_errors.append(str(exc))
            print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
            app.exit(1)
            return
        window._show_page(0)
        navigate_next()

    def navigate_next() -> None:
        if window.current == 6:
            if window.manifest.to_dict(redact=False) != expected_manifest:
                print(json.dumps({"type": "error", "error": "Wizard changed the preloaded VM manifest"}), flush=True)
                app.exit(1)
                return
            if (window.manifest.storage.device != "/dev/vda" or
                    window.manifest.storage.device_id != "GENTOO-VM-DISPOSABLE" or
                    window.storage.disk_group.value() != "/dev/vda"):
                print(json.dumps({"type": "error", "error": "Review target does not match the disposable VM disk"}), flush=True)
                app.exit(1)
                return
            window.review.confirmation.setText("/dev/vda")
            try:
                save_screenshot(args.output / "review.png")
            except RuntimeError as exc:
                print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
                app.exit(1)
                return
            print(json.dumps({"type": "gui_navigation", "pages": list(range(7)),
                              "manifest_matches": True, "target": "/dev/vda",
                              "device_id": "GENTOO-VM-DISPOSABLE"}), flush=True)
            QTimer.singleShot(100, confirm)
            window.next.click()
            worker = window.progress.worker
            if worker is None:
                print(json.dumps({"type": "error", "error": "GUI did not start the backend worker"}), flush=True)
                app.exit(1)
            return
        current = window.current
        QTimer.singleShot(100, fail_modal)
        window.next.click()
        if window.current != current + 1:
            # A validation dialog is handled by fail_modal; if navigation
            # stopped without one, fail closed instead of bypassing a page.
            QTimer.singleShot(150, lambda: navigation_stalled(current))
            return
        QTimer.singleShot(100, navigate_next)

    def navigation_stalled(previous: int) -> None:
        if window.current == previous:
            print(json.dumps({"type": "error", "error": f"Continue did not advance from page {previous}"}), flush=True)
            app.exit(1)


    window.progress.finished.connect(finish)
    QTimer.singleShot(200, start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
