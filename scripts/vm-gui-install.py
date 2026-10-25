#!/usr/bin/env python3
"""Guest-only GUI acceptance driver; uses the real client and root daemon.

Run as the live gentoo user inside the disposable QEMU VM. No installer mocks
or privileged subprocesses are used. The wizard is traversed through its real
Continue buttons; only the final review confirmation is automated.

With a plan (``--plan``, written by scripts/test-vm.py) the driver

* proves again, as the unprivileged user, that the disk it is about to let the GUI erase is
  the harness's disk and that no other disk is attached (scripts/vm_guard.py);
* in detect mode starts from the wizard's defaults plus the plan's overrides, leaves the
  firmware, the partition table and the disk to the GUI's own detection, selects the disk like
  a user, and requires the wizard's final manifest to equal the expected one, so a GUI that
  detects the wrong firmware or disk fails the run;
* is bounded by watchdogs: a page that never advances, a backend that goes silent, or a live
  system whose network never comes up end the run with exit status 2 instead of hanging.

Without a plan it behaves as before: /dev/vda with serial GENTOO-VM-DISPOSABLE, pre-filled.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Slot

LEGACY_DEVICE = "/dev/vda"
LEGACY_SERIAL = "GENTOO-VM-DISPOSABLE"
SECRET_KEYS = {"password", "root_password", "luks_passphrase", "wifi_password", "proxy_password"}
PLAN_KEYS = {"guard", "detect", "target", "overrides", "detected"}
WATCHDOG_STATUS = 2


# --- the plan --------------------------------------------------------------------------------------

def load_plan(path: Path | None) -> dict:
    """Read the harness's plan; refuse keys it does not know (a typo must not silently disable a check)."""
    if path is None:
        return {"guard": None, "detect": False, "target": None, "overrides": {}, "detected": {}}
    plan = json.loads(Path(path).read_text())
    unknown = set(plan) - PLAN_KEYS
    if unknown:
        raise ValueError("Unknown plan keys: " + ", ".join(sorted(unknown)))
    return {"guard": plan.get("guard"), "detect": bool(plan.get("detect")), "target": plan.get("target"),
            "overrides": plan.get("overrides") or {}, "detected": plan.get("detected") or {}}


def merge_overrides(defaults: dict, overrides: dict) -> dict:
    """The wizard's default manifest with what a person would type in; unknown sections or fields are errors."""
    merged = json.loads(json.dumps(defaults))
    for section, values in overrides.items():
        if section not in merged or not isinstance(merged[section], dict) or not isinstance(values, dict):
            raise ValueError(f"Unknown manifest section in the overrides: {section}")
        unknown = set(values) - set(merged[section])
        if unknown:
            raise ValueError(f"Unknown {section} fields in the overrides: {', '.join(sorted(unknown))}")
        merged[section].update(values)
    return merged


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        result: dict[str, Any] = {}
        for key, item in value.items():
            result.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return result
    return {prefix: value}


def _show(path: str, value: Any) -> str:
    return "<secret>" if path.rsplit(".", 1)[-1] in SECRET_KEYS and value else repr(value)


def compare_manifests(expected: dict, actual: dict, detected: dict[str, Any] | None = None,
                      target: dict | None = None) -> list[str]:
    """How the wizard's manifest differs from the expected one (empty: it matches).

    Every field must be equal except the ``detected`` ones, which the GUI chose itself:
    those must equal the plan's expectation when it names one (None: recorded, not asserted).
    A disk identity may be a serial (kind "" or "serial") or, for a disk without one, a
    ``by-path|size|model`` triple whose by-path name only has to be one of the disk's links.
    """
    detected = detected or {}
    want, got = flatten(expected), flatten(actual)
    problems = [f"{path}: expected {_show(path, want.get(path))}, the wizard has {_show(path, got.get(path))}"
                for path in sorted(set(want) | set(got)) if path not in detected and want.get(path) != got.get(path)]
    for path, wanted in sorted(detected.items()):
        found = got.get(path)
        if wanted is None:
            continue
        if path == "storage.device_id_kind":
            same = (found or "serial") == (wanted or "serial")
        elif path == "storage.device_id" and got.get("storage.device_id_kind") == "path":
            same = _same_path_identity(str(found), str(wanted), target)
        else:
            same = found == wanted
        if not same:
            problems.append(f"{path}: the GUI chose {found!r}, expected {wanted!r}")
    return problems


def _same_path_identity(found: str, wanted: str, target: dict | None) -> bool:
    got, want = found.split("|"), wanted.split("|")
    if len(got) != 3 or len(want) != 3 or got[1:] != want[1:]:
        return False
    return got[0] == want[0] or got[0] in ((target or {}).get("by_path") or [])


# --- readiness ---------------------------------------------------------------------------------------

def readiness_problems(info: Any, firmware: str, target: dict | None = None) -> tuple[list[str], list[str]]:
    """Split the live scan into (fatal, transient) problems.

    Identity and safety problems (the disposable disk missing, changed, mounted or read-only,
    the wrong architecture or firmware) are fatal and never retried. Network and tools may still
    resolve while NetworkManager and DHCP finish starting, so they are retried by the caller.
    """
    target = target or {"path": LEGACY_DEVICE, "serial": LEGACY_SERIAL}
    fatal: list[str] = []
    transient: list[str] = []
    disk = next((item for item in info.disks if item.path == target["path"]), None)
    identity = (getattr(disk, "wwid", "") or getattr(disk, "serial", "")) if disk else ""
    if (disk is None or (target.get("serial") and identity != target["serial"])
            or (target.get("size") and disk.size != target["size"])
            or getattr(disk, "read_only", False) or getattr(disk, "mounted", False)):
        fatal.append(f"Live scan did not identify an available disposable {target['path']}")
    if info.architecture not in {"x86_64", "amd64"}:
        fatal.append(f"Unsupported live architecture: {info.architecture}")
    if bool(info.uefi) != (firmware == "uefi"):
        fatal.append("The expected firmware does not match the live VM firmware")
    if not info.network:
        transient.append("network is not reachable")
    if info.missing_tools:
        transient.append("missing tools: " + ", ".join(info.missing_tools))
    return fatal, transient


def guard_problems(spec_dict: dict | None) -> list[str]:
    """The independent disk check, run as the unprivileged user (the nonce needs root and is skipped)."""
    if not spec_dict:
        return []
    for directory in (str(Path(__file__).resolve().parent), "/run"):  # the harness uploads vm_guard.py to /run
        if directory not in sys.path:
            sys.path.append(directory)
    import vm_guard
    spec = vm_guard.GuardSpec(
        disks=[vm_guard.DiskExpectation(**disk) for disk in spec_dict["disks"]], nonce=spec_dict.get("nonce", ""),
        allow_unlisted=bool(spec_dict.get("allow_unlisted")))
    return list(vm_guard.collect(spec)["problems"])


# --- watchdog ----------------------------------------------------------------------------------------

class Watchdog:
    """One deadline at a time: arm() replaces it, stop() clears it, expiry calls ``on_expire(message)``."""

    def __init__(self, on_expire: Callable[[str], None]) -> None:
        self.on_expire = on_expire
        self.message = ""
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._expired)

    def arm(self, seconds: float, reason: str) -> None:
        self.message = f"Watchdog: {reason} for {seconds:g}s"
        self.timer.start(max(1, int(seconds * 1000)))

    def stop(self) -> None:
        self.timer.stop()

    def _expired(self) -> None:
        self.on_expire(self.message)


# --- the driver --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, help="The expected manifest (the harness writes it as manifest.json)")
    parser.add_argument("--output", type=Path, default=Path("/tmp/gentoo-vm-gui"))
    parser.add_argument("--plan", type=Path, help="The harness's plan: guard, detect mode, overrides, expected detections")
    parser.add_argument("--socket", default="/run/gentoo-installer/server.sock",
                        help="The installer daemon's socket (tests point this at a fake daemon)")
    parser.add_argument("--cancel-at-stage3", action="store_true",
                        help="Click the real GUI cancel button when the backend starts stage3")
    parser.add_argument("--readiness-timeout", type=int, default=60,
                        help="Seconds to retry the network/tools readiness scan before failing (identity checks never retry)")
    parser.add_argument("--navigation-timeout", type=int, default=120,
                        help="Seconds a wizard page may take to advance before the driver exits with status 2")
    parser.add_argument("--idle-timeout", type=int, default=1800,
                        help="Seconds without any backend event during the install before the driver exits with status 2")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Refusing GUI automation: unusable plan: {exc}")
    target = plan["target"] or {"path": LEGACY_DEVICE, "serial": LEGACY_SERIAL}
    serial = Path("/sys/class/block") / Path(target["path"]).name / "serial"
    if not serial.exists():
        serial = serial.parent / "device" / "serial"
    legacy_identity_ok = plan["guard"] is not None or (serial.is_file() and serial.read_text().strip() == LEGACY_SERIAL)
    if os.geteuid() == 0 or not legacy_identity_ok:
        raise SystemExit("Refusing GUI automation outside the non-root disposable VM session")
    display = os.environ.get("DISPLAY", "")
    authority = Path(os.environ.get("XAUTHORITY", ""))
    if not re.fullmatch(r"(?::[0-9]+(?:\.[0-9]+)?|unix:[0-9]+(?:\.[0-9]+)?)", display):
        raise SystemExit("Refusing GUI automation without a local X11 DISPLAY")
    if not authority.is_absolute() or not authority.is_file() or not os.access(authority, os.R_OK):
        raise SystemExit("Refusing GUI automation without a readable XAUTHORITY file")
    problems = guard_problems(plan["guard"])
    if problems:
        raise SystemExit("Refusing GUI automation: the disk guard reports " + "; ".join(problems))
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gentoo_installer.app import InstallerWindow
    from gentoo_installer.backend import BackendWorker
    from gentoo_installer.manifest import InstallManifest
    from gentoo_installer.theme import apply_theme

    expected_manifest = InstallManifest.load(args.manifest)
    expected = expected_manifest.to_dict(redact=False)
    device, device_id = expected_manifest.storage.device, expected_manifest.storage.device_id
    if plan["guard"] is None and (device != LEGACY_DEVICE or device_id != LEGACY_SERIAL):
        raise SystemExit("Manifest does not select the disposable VM disk")
    if plan["target"] is not None and device != plan["target"]["path"]:
        raise SystemExit("Manifest does not select the disk the guard found")
    detect = plan["detect"]
    try:
        initial = (InstallManifest.from_dict(merge_overrides(InstallManifest().to_dict(redact=False), plan["overrides"]))
                   if detect else InstallManifest.load(args.manifest))
    except ValueError as exc:
        raise SystemExit(f"Refusing GUI automation: {exc}")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(["gentoo-installer-vm-test"])
    apply_theme(app)

    def save_screenshot(path: Path) -> None:
        if not window.grab().save(str(path)) or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Could not save nonempty screenshot: {path}")

    def fail(message: str, status: int = 1, **extra: Any) -> None:
        print(json.dumps({"type": "error", "error": message, **extra}), flush=True)
        app.exit(status)

    watchdog = Watchdog(lambda message: fail(message, WATCHDOG_STATUS))
    closed = [False]

    def later(milliseconds: int, callback: Callable[[], None]) -> None:
        """QTimer.singleShot that does nothing once this run is over (a test runs the driver in-process)."""
        QTimer.singleShot(milliseconds, lambda: None if closed[0] else callback())

    original_worker_init = BackendWorker.__init__
    cancel_state = {"requested": False, "storage_completed": False, "stage3_started": 0}

    class EventObserver(QObject):
        @Slot(dict)
        def on_event(self, event: dict) -> None:
            watchdog.arm(args.idle_timeout, "no backend event")
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
                    # The Cancel button asks for confirmation first; this is the confirmed path.
                    window.progress.request_cancel()

    observer = EventObserver()

    def observed_worker_init(worker, *worker_args, **worker_kwargs):
        original_worker_init(worker, *worker_args, **worker_kwargs)
        worker.eventReceived.connect(observer.on_event)

    setattr(BackendWorker, "__init__", observed_worker_init)
    # A preloaded manifest names the firmware; in detect mode the GUI picks it from the boot mode.
    window = InstallerWindow(initial, args.socket, firmware_explicit=not detect)
    window.show()
    outcome: list[bool] = []

    def finish(success: bool) -> None:
        watchdog.stop()
        outcome.append(success)
        print(json.dumps({"type": "gui_result", "success": success,
                          "stage": window.progress.stage.text(),
                          "cancel_requested": cancel_state["requested"],
                          "storage_completed": cancel_state["storage_completed"],
                          "stage3_started": cancel_state["stage3_started"],
                          "reboot_enabled": not window.progress.reboot.isHidden()}), flush=True)
        later(100, save_and_exit)

    def save_and_exit() -> None:
        worker = window.progress.worker
        if worker is not None and worker.isRunning():
            later(100, save_and_exit)
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
        expected_outcome = (outcome == [False] and cancel_state["requested"] and
                            cancel_state["storage_completed"] and cancel_state["stage3_started"] == 1)
        app.exit(0 if (expected_outcome if args.cancel_at_stage3 else outcome == [True]) else 1)

    def confirm() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            yes = dialog.button(QMessageBox.StandardButton.Yes)
            if yes is not None and dialog.windowTitle() == "Erase the selected disk?":
                yes.click()
            else:
                fail(dialog.text())

    def fail_modal() -> None:
        dialog = app.activeModalWidget()
        if isinstance(dialog, QMessageBox):
            text, title = dialog.text(), dialog.windowTitle()
            dialog.reject()
            fail(text, dialog=title)

    def readiness_text() -> str:
        return json.dumps({key: row.accessibleDescription() for key, row in window.readiness.rows.items()})

    def start() -> None:
        # The live disk/readiness scan runs in the background (also after refresh_readiness()),
        # so wait until it reports, retry only the transient (network, tools) problems, then
        # proceed through the real wizard.
        deadline = time.monotonic() + args.readiness_timeout

        def check() -> None:
            if not window.info.inspected:
                if time.monotonic() < deadline:
                    later(200, check)
                else:
                    fail(f"The live readiness scan did not finish within {args.readiness_timeout}s")
                return
            fatal, transient = readiness_problems(window.info, expected_manifest.storage.firmware, plan["target"])
            fatal += guard_problems(plan["guard"])
            if fatal:
                fail("; ".join(fatal))
                return
            if transient:
                if time.monotonic() < deadline:
                    window.refresh_readiness()
                    later(2000, check)
                    return
                fail("Live readiness scan still reports " + "; ".join(transient) +
                     f" after {args.readiness_timeout}s: {readiness_text()}")
                return
            window._show_page(0)
            navigate_next()

        check()

    chosen: dict[str, Any] = {}

    def select_disk() -> None:
        """Choose the disk like a user does; remember what the GUI had selected by itself."""
        chosen["preselected"] = window.storage.disk_group.value()
        window.storage.disk_group.set_value(device)
        approval = window.storage.approve_path
        if approval.isVisibleTo(window.storage):  # no serial number: a user has to approve the disk explicitly
            approval.setChecked(True)

    def navigate_next() -> None:
        if window.current == 6:
            actual = window.manifest.to_dict(redact=False)
            differences = compare_manifests(expected, actual, plan["detected"] if detect else {}, plan["target"])
            if differences:
                fail("Wizard changed the " + ("detected" if detect else "preloaded VM") + " manifest: " + "; ".join(differences))
                return
            if window.manifest.storage.device != device or window.storage.disk_group.value() != device:
                fail("Review target does not match the disposable VM disk")
                return
            chosen.update({key: flatten(actual).get(key) for key in plan["detected"]})
            window.review.confirmation.setText(device)
            try:
                save_screenshot(args.output / "review.png")
                if detect:
                    (args.output / "detected.json").write_text(json.dumps(chosen, indent=2, sort_keys=True) + "\n")
            except (RuntimeError, OSError) as exc:
                fail(str(exc))
                return
            print(json.dumps({"type": "gui_navigation", "pages": list(range(7)), "manifest_matches": True,
                              "target": device, "device_id": device_id, "detect": detect, "detected": chosen}), flush=True)
            later(100, confirm)
            watchdog.arm(args.navigation_timeout, "the backend did not start")
            window.next.click()
            worker = window.progress.worker
            if worker is None:
                fail("GUI did not start the backend worker")
            return
        current = window.current
        if detect and current == 2:
            select_disk()  # the storage page has loaded its defaults by now: choose like a user
        later(100, fail_modal)
        watchdog.arm(args.navigation_timeout, f"page {current} did not advance")
        window.next.click()
        shown = window.pages[current]
        if window.current == current and shown.has_problem():  # a page now shows its problem inline instead of in a dialog
            fail(shown.problem_label.text(), dialog="Check this step")
            return
        if window.current != current + 1:
            # A validation dialog is handled by fail_modal; if navigation
            # stopped without one, fail closed instead of bypassing a page.
            later(150, lambda: navigation_stalled(current))
            return
        watchdog.stop()
        later(100, navigate_next)

    def navigation_stalled(previous: int) -> None:
        if window.current == previous:
            fail(f"Continue did not advance from page {previous}")

    window.progress.finished.connect(finish)
    later(200, start)
    try:
        return app.exec()
    finally:  # leave the process as it was (a test runs this in-process)
        closed[0] = True
        watchdog.stop()
        setattr(BackendWorker, "__init__", original_worker_init)


if __name__ == "__main__":
    raise SystemExit(main())
