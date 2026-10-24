"""What to offer when an installation has ended: restart, resume, read the log, start over."""

from __future__ import annotations

import os
import time

from .console import Console, InputClosed, Option, Quit, clip
from .daemon import Daemon, DaemonError
from .follow import DEFAULT_LOG_PATH, Outcome, stage_label
from .host import Host

LOG_TAIL_LINES = 40


def describe(console: Console, outcome: Outcome) -> None:
    """Print how the installation ended (the same text for interactive and unattended runs)."""
    c = console
    c.say()
    if outcome.ok:
        c.say(c.style("The installation finished successfully.", "green", "bold"))
        if outcome.degraded:
            c.para("Some optional parts could not be installed. The system works without them; you can add them later:")
            for item in outcome.degraded:
                c.para(f"- {item}", indent=2, hanging=4)
        for warning in outcome.warnings:
            c.warn(warning)
        c.para("Remove the installation medium (USB stick or disc), then restart the computer.")
        return
    where = f" while: {stage_label(outcome.stage)}" if outcome.stage else ""
    if outcome.state == "cancelled":
        c.say(c.style("The installation was cancelled" + where + ".", "yellow", "bold"))
    elif outcome.state == "busy":
        c.say(c.style("Another installation is already running on this computer.", "yellow", "bold"))
    elif outcome.state == "blocked":
        c.say(c.style("The installation cannot start yet.", "yellow", "bold"))
    elif outcome.state == "invalid":
        c.say(c.style("The installer service refused the settings.", "red", "bold"))
    elif outcome.state == "detached":
        c.say(c.style("You stopped watching. The installation is still running.", "yellow", "bold"))
    elif outcome.state == "unreachable":
        c.say(c.style("Lost contact with the installer service.", "red", "bold"))
    else:
        c.say(c.style("The installation stopped" + where + ".", "red", "bold"))
    if outcome.message:
        c.para(outcome.message, indent=2)
    if outcome.state == "blocked":
        c.para("Nothing was started and nothing on your disks was changed. Close the repair tool (or unmount what it holds), then start again.")
    if outcome.code == "storage.holders":
        c.para("No data was written to your disks: the installer stopped before it started to prepare them.")
    if outcome.state in {"failed", "cancelled", "unreachable"}:
        c.para(f"The installation log is on this computer: {outcome.log_path or DEFAULT_LOG_PATH} (only root can read that file; the menu below can show it).")
    if outcome.bootable:
        c.para("The new system was installed far enough to start. Some parts are missing; you can restart into it now and add them later, or resume.")
    if outcome.state == "detached":
        c.para("Follow it again with: gentoo-installer-text --attach")


def tell_how_to_resume(console: Console, outcome: Outcome) -> None:
    """For unattended runs: what to type next."""
    if outcome.resumable:
        console.para("The disk was already prepared. To continue where it stopped without erasing the disk again, run the same command with --resume.")
    if outcome.code == "storage.holders":
        console.para("To let the installer switch these volumes off and destroy them, run the same command with --release-holders.")


def confirm_release(console: Console) -> bool:
    """The service refused to erase a disk with volumes, swap or RAID/LVM members alive on it: ask whether they may be destroyed."""
    console.para("Switching them off destroys their data, and also data stored on other disks of the same volume group, RAID array, Btrfs or ZFS pool.")
    return console.confirm("Really switch them off and erase them together with the disk?", default=False, back=False)


def show_log(console: Console, daemon: Daemon) -> None:
    try:
        lines = daemon.log_lines(tail=LOG_TAIL_LINES)
    except (OSError, DaemonError) as exc:
        console.warn(f"The log could not be read from the installer service: {exc}")
        return
    if not lines:
        console.say("There is no installation log yet.")
        return
    console.say(f"The last {len(lines)} lines of the installation log:")
    console.show_lines([clip(line, console.width) for line in lines])


def save_log(console: Console, daemon: Daemon, host: Host) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(host.now()))
    path = host.home_dir() / f"gentoo-installer-{stamp}.log"
    try:
        lines = daemon.log_lines()
    except (OSError, DaemonError) as exc:
        console.warn(f"The log could not be read from the installer service: {exc}")
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + ("\n" if lines else ""))
    except OSError as exc:
        console.warn(f"Could not write {path}: {exc.strerror or exc}")
        return
    host.give_to_invoker(path)
    console.good(f"Saved {len(lines)} log lines to {path}. Copy it to a USB stick before you restart if you want to keep it: this live system forgets everything.")


def restart(console: Console, host: Host) -> bool:
    ok, why = host.reboot()
    if ok:
        console.say("Restarting...")
        return True
    console.warn(f"The computer could not be restarted automatically ({why}). Type: reboot")
    return False


def conclude(console: Console, host: Host, daemon: Daemon, outcome: Outcome, *, can_resume: bool = True, can_release: bool = True) -> str:
    """Show the result and offer what makes sense. Returns "resume", "release", "again", "attach" or "done".

    ``can_resume`` is false when the settings of the installation are not known (a run that was only attached to).
    ``can_release`` is false when they are not known, or when they already carry the consent to switch volumes off."""
    describe(console, outcome)
    while True:
        options: list[Option] = []
        actions: list[str] = []

        def offer(label: str, action: str, detail: str = "") -> None:
            options.append(Option(label, detail))
            actions.append(action)

        if outcome.ok:
            offer("Restart the computer now", "restart", "Remove the installation medium first, or choose it in the boot menu again.")
        if outcome.state in {"busy", "detached", "unreachable"}:
            offer("Follow the running installation", "attach")
        if outcome.state in {"failed", "cancelled"} and outcome.resumable and can_resume:
            offer("Resume the installation", "resume", "Continues where it stopped. The disk is not erased again.")
        if outcome.bootable and not outcome.ok:
            offer("Restart the computer now", "restart", "The system installed so far can start. Remove the installation medium first.")
        if outcome.state == "failed" and outcome.code == "storage.holders" and can_release:
            offer("Switch them off, erase them together with the disk, and try again", "release",
                  "Their data is lost, and so is data stored on other disks of the same volume group, RAID array, Btrfs or ZFS pool.")
        if outcome.state in {"failed", "cancelled", "invalid", "blocked"}:
            hint = {"invalid": "", "blocked": "Start again from the summary once the repair tool has let go of the disks."}
            default = "Change a setting or choose another disk. Nothing has been erased." if outcome.code == "storage.holders" else "Change a setting, or start over. Starting over erases the disk again."
            offer("Go back to the summary", "again", hint.get(outcome.state, default))
        offer("Show the last lines of the installation log", "log")
        offer("Save the installation log to a file", "save")
        offer("Quit", "done")
        preferred = next((actions.index(name) for name in ("restart", "attach", "resume") if name in actions), None)
        try:
            choice = console.menu(options, default=preferred, back=False, prompt="What now")
        except (Quit, InputClosed):  # nobody answers any more: the result of the installation is what counts
            return "done"
        action = actions[choice.index]
        if action == "log":
            show_log(console, daemon)
        elif action == "save":
            save_log(console, daemon, host)
        elif action == "restart":
            if restart(console, host):
                return "done"
        elif action == "release":
            if confirm_release(console):
                return action
        else:
            return action
