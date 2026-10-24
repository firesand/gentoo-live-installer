"""The questions of the console installer.

The wizard fills an ``InstallManifest`` step by step: keyboard, network, the hardware report,
disk, filesystem and encryption, desktop and init system, accounts, time zone and language. Then
it shows a summary and asks for the typed confirmation of the disk. Every answer is checked
against the manifest's own rules as soon as it is given, and every step can go back.

It never touches a disk: the installation is started through the daemon by the caller.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Sequence

from .. import blockdev, hardware, keymaps, layout, recommend
from ..daemon_client import DaemonState
from ..diagnostics import redact_url_userinfo
from ..hardware import NicInfo
from ..layout import human_size
from ..manifest import KEYMAP_RE, InstallManifest
from .check import field_errors
from .console import Console, GoBack, Option, Quit
from .daemon import Daemon, DaemonError
from .host import Host
from .inventory import DiskEntry, Finding, MachineView, apply_disk
from .summary import build_summary, keyboard_label, render_summary

MAX_WRONG_CONFIRMATIONS = 3
NO_IDENTITY_HINT = "A virtual disk can be given a serial number, for example QEMU: -device virtio-blk-pci,drive=d0,serial=MYDISK."
FIRST_STEP_NOTE = "This is the first step; there is nothing before it."
RESERVED_USERNAMES = frozenset({
    "root", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt", "operator", "nobody", "portage", "sshd", "messagebus", "polkitd",
})

DESKTOPS = (
    ("kde", "KDE Plasma", "A complete, polished desktop with many applications; it needs the most memory."),
    ("gnome", "GNOME", "A modern, simple desktop."),
    ("xfce", "Xfce", "A light, fast desktop. A good choice for older computers."),
    ("minimal", "Minimal system (no desktop)", "Only the base system and a command line. You add what you need later."),
)
INITS = (
    ("openrc", "OpenRC", "Gentoo's traditional init system: simple and fast. Recommended."),
    ("systemd", "systemd", "The init system most other Linux distributions use."),
)
FILESYSTEMS = (
    ("ext4", "ext4", "The standard Linux filesystem. Reliable; a good choice for most people."),
    ("xfs", "XFS", "Good with very large files and disks."),
    ("btrfs", "Btrfs", "Snapshots and compression. Somewhat more complex."),
)
BUS_NAMES = {"nvme": "NVMe", "sata": "SATA", "ata": "SATA", "usb": "USB", "sas": "SAS", "scsi": "SCSI", "virtio": "virtual"}
STAGE_FAMILIES = ("amd64", "amd64-desktop", "amd64-nomultilib", "amd64-hardened")
TIMEZONE_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian", "Pacific")
# The language that goes with a keyboard layout: offered as the default when the locale is still the stock one.
LOCALE_FOR_LAYOUT = {
    "gb": "en_GB.UTF-8", "de": "de_DE.UTF-8", "at": "de_AT.UTF-8", "ch": "de_CH.UTF-8", "fr": "fr_FR.UTF-8", "be": "fr_BE.UTF-8",
    "es": "es_ES.UTF-8", "latam": "es_MX.UTF-8", "it": "it_IT.UTF-8", "pt": "pt_PT.UTF-8", "br": "pt_BR.UTF-8", "nl": "nl_NL.UTF-8",
    "se": "sv_SE.UTF-8", "dk": "da_DK.UTF-8", "no": "nb_NO.UTF-8", "fi": "fi_FI.UTF-8", "pl": "pl_PL.UTF-8", "cz": "cs_CZ.UTF-8",
    "hu": "hu_HU.UTF-8", "ro": "ro_RO.UTF-8", "tr": "tr_TR.UTF-8", "jp": "ja_JP.UTF-8", "ru": "ru_RU.UTF-8", "ua": "uk_UA.UTF-8",
    "gr": "el_GR.UTF-8", "kr": "ko_KR.UTF-8", "cn": "zh_CN.UTF-8",
}


@dataclass
class StartPlan:
    """The user confirmed: start the installation (``resume``: continue an interrupted one)."""

    resume: bool = False


@dataclass
class Jump:
    """Go back to a step to change a setting."""

    index: int


@dataclass
class Refused:
    """The typed confirmation was wrong too often; nothing was started."""

    reason: str


def first_problem(problems: Sequence[str]) -> str:
    return problems[0] if problems else ""


def nic_text(nic: NicInfo) -> str:
    name = nic.iface or "(no device)"
    if nic.no_driver:
        return f"{name}: an adapter without a driver; it will not work (it may need firmware)"
    if nic.kind == "wifi":
        return f"{name}: Wi-Fi"
    if nic.kind == "ethernet":
        state = "" if nic.carrier is None else " (cable plugged in)" if nic.carrier else " (no cable)"
        return f"{name}: network cable{state}"
    return f"{name}: {nic.kind}"


def disk_option(entry: DiskEntry) -> Option:
    """One line for the disk menu: what the disk is, and what is on it today."""
    disk = entry.disk
    bus = BUS_NAMES.get(disk.transport.lower(), disk.transport.upper())
    facts = ", ".join(part for part in (human_size(disk.size), bus, layout.identity_text(disk)) if part)
    flags = [name for name, active in (("removable", disk.removable or disk.hotplug), ("mounted", disk.mounted)) if active]
    windows = " Looks like Windows is installed here." if any(node.fstype in {"ntfs", "bitlocker"} for node in disk.walk()) else ""
    detail = f"{facts}{' (' + ', '.join(flags) + ')' if flags else ''}\nNow on it: {layout.contents_text(disk)}.{windows}"
    return Option(f"{disk.path} - {disk.model or 'unnamed disk'}", detail)


class Wizard:
    """Asks the questions and keeps the manifest and the picture of this computer."""

    def __init__(
        self,
        console: Console,
        host: Host,
        daemon: Daemon,
        *,
        manifest: InstallManifest | None = None,
        view: MachineView | None = None,
        state: DaemonState | None = None,
        from_file: bool = False,
        resume: bool = False,
    ) -> None:
        self.console = console
        self.host = host
        self.daemon = daemon
        self.manifest = manifest if manifest is not None else InstallManifest()
        self.view = view if view is not None else MachineView()
        self.state = state
        self.from_file = from_file
        self.resume_requested = resume  # --resume: continue the interrupted installation instead of asking
        self.entry: DiskEntry | None = None
        self.applied_keymap = ""
        self.root_mode = "off" if self.manifest.accounts.disable_root_login else "same"
        self.steps: list[tuple[str, Callable[[], None]]] = [
            ("Keyboard", self.step_keyboard),
            ("Network", self.step_network),
            ("This computer", self.step_hardware),
            ("Disk", self.step_disk),
            ("Filesystem and encryption", self.step_storage),
            ("Desktop and init system", self.step_system),
            ("Accounts", self.step_accounts),
            ("Time zone and language", self.step_region),
        ]
        self.apply_machine_defaults()

    # --- the machine ----------------------------------------------------------------------

    def apply_machine_defaults(self) -> None:
        """Pre-select what the hardware tells (boot mode, swap size, Bluetooth, microcode in a VM)."""
        if self.from_file:
            return
        if self.view.hardware:
            recommend.apply_defaults(self.manifest, self.view.hardware)
        if self.view.uefi is not None:  # also when the hardware report is missing: UEFI on a BIOS PC cannot boot
            self.manifest.storage.firmware = "uefi" if self.view.uefi else "bios"

    def refresh_view(self) -> bool:
        """Ask the daemon about this computer again. False (and a message) when it cannot answer."""
        self.console.say("Checking this computer...")
        try:
            reply = self.daemon.inspect(self.manifest.network.proxy)
        except (OSError, DaemonError) as exc:
            self.console.warn(f"The installer service could not check this computer: {exc}")
            return False
        self.view = MachineView(reply, local_by_path=self.host.by_path_names())
        self.apply_machine_defaults()
        if self.entry is not None:
            self.entry = self.view.find_disk(self.entry.disk.path)
        return True

    def can_ask_daemon_for(self, action: str) -> bool:
        return self.state is not None and self.state.supports(action)

    def current_disk(self) -> DiskEntry | None:
        return self.view.find_disk(self.manifest.storage.device) if self.manifest.storage.device else None

    # --- the walk -----------------------------------------------------------------------------

    def intro(self) -> None:
        c = self.console
        c.say()
        c.say(c.style("Gentoo installer - text mode", "bold"))
        c.para("This program installs Gentoo Linux on this computer. It asks a few questions. Nothing on your disks is changed until the very end, after you have read a summary and typed the name of the disk.")
        c.para("At every question: b (or <) = go back, ? = help, Ctrl-C = quit. Enter accepts the answer in [brackets].")

    def run(self, *, start_at_review: bool = False) -> StartPlan | Refused:
        """Walk through the steps and the review. Raises Quit when the user leaves."""
        self.intro()
        index = len(self.steps) if start_at_review else 0
        total = len(self.steps) + 1
        while True:
            if index < len(self.steps):
                title, step = self.steps[index]
                self.console.heading(f"Step {index + 1} of {total}: {title}")
                try:
                    step()
                except GoBack:
                    if index == 0:
                        self.console.say(FIRST_STEP_NOTE)
                    else:
                        index -= 1
                    continue
                except KeyboardInterrupt:  # Ctrl-C while the program was busy (asking the service), not waiting at a prompt
                    self._ask_to_quit()
                    continue
                index += 1
                continue
            self.console.heading(f"Step {total} of {total}: Review and start")
            try:
                result = self.review()
            except GoBack:
                index = len(self.steps) - 1
                continue
            except KeyboardInterrupt:
                self._ask_to_quit()
                continue
            if isinstance(result, Jump):
                index = result.index
                continue
            return result

    def _ask_to_quit(self) -> None:
        """Ctrl-C outside a prompt: the same question as at a prompt. Returns when the user wants to go on."""
        self.console.say()
        if self.console.confirm_quit():
            raise Quit

    def substeps(self, *parts: Callable[[], bool]) -> None:
        """Run the questions of one step in order. A part returns whether it asked anything; going back skips parts that did not."""
        asked: list[bool] = []
        position = 0
        while position < len(parts):
            try:
                did_ask = parts[position]()
            except GoBack:
                earlier = [i for i in range(position) if asked[i]]
                if not earlier:
                    raise
                position = earlier[-1]
                asked = asked[:position]
                continue
            asked.append(did_ask)
            position += 1

    # --- 1. keyboard --------------------------------------------------------------------------------

    def step_keyboard(self) -> None:
        c = self.console
        c.para("Choose your keyboard layout. It is used on this text console now, on the installed system later, and when you type a disk passphrase while the computer starts.")
        presets = keymaps.PRESETS
        labels = [preset.label for preset in presets] + ["Another layout (type its name)"]
        system = self.manifest.system
        current = next((i for i, p in enumerate(presets) if (p.console, p.layout, p.variant) == (system.keymap, system.keyboard_layout, system.keyboard_variant)), 0)
        while True:
            index = c.pick(labels, default=current, help="Pick the layout that matches the letters printed on your keys. If yours is not listed, choose the last entry.")
            if index == len(presets):
                choice = self._other_keyboard()
                if choice is None:
                    continue
            else:
                preset = presets[index]
                choice = (preset.console, preset.layout, preset.variant)
            self._apply_keyboard(*choice)
            if self._keyboard_looks_right():
                return
            current = index

    def _other_keyboard(self) -> tuple[str, str, str] | None:
        c = self.console
        installed = self.host.console_keymaps()

        def check_map(name: str) -> str:
            if not KEYMAP_RE.fullmatch(name):
                return "A keymap name has letters, digits and . _ + - only, for example de-latin1."
            if installed and name not in installed:
                near = keymaps.near_matches(name, sorted(installed))
                return f"This system has no keymap called {name}." + (f" Did you mean: {', '.join(near)}?" if near else "")
            return ""

        c.para("The console keymap is the name of a keyboard layout for the text console, for example de-latin1, fr, uk or dvorak.")
        name = c.ask("Console keymap name", validate=check_map)
        preset = keymaps.preset_for_console(name)
        c.para("The desktop uses a layout with a short name, such as de, fr or us.")
        layout_name = c.ask(
            "Desktop keyboard layout", default=preset.layout if preset else "us",
            validate=lambda value: first_problem(field_errors("locale", lambda m: setattr(m.system, "keyboard_layout", value))),
        )
        variant = c.ask(
            "Layout variant (Enter for none)", default=preset.variant if preset and preset.layout == layout_name else "", allow_empty=True,
            validate=lambda value: first_problem(field_errors("locale", lambda m: setattr(m.system, "keyboard_variant", value))),
        )
        return name, layout_name, variant

    def _apply_keyboard(self, console_map: str, layout_name: str, variant: str) -> None:
        """Set keymap, layout and variant together (the boot prompt relies on them agreeing) and switch this console."""
        system = self.manifest.system
        system.keymap, system.keyboard_layout, system.keyboard_variant = console_map, layout_name, variant
        if console_map == self.applied_keymap:
            return
        ok, why = self.host.loadkeys(console_map)
        if ok:
            self.applied_keymap = console_map
            self.console.good(f"This console now uses the keymap {console_map} ({keyboard_label(self.manifest)}).")
        else:
            self.console.note(f"The keyboard of this console was not switched ({why}). That is normal over a serial line or SSH. The choice is still used for the installed system.")

    def _keyboard_looks_right(self) -> bool:
        if self.manifest.system.keymap == "us":
            return True
        self.console.para("Test the layout: type a few keys such as y z @ ' \" - and press Enter.")
        self.console.ask("Try your keys", allow_empty=True)
        return self.console.confirm("Do the characters match the keys you pressed?", default=True)

    # --- 2. network -----------------------------------------------------------------------------------

    def step_network(self) -> None:
        c = self.console
        while True:
            connected = self.view.network is True
            lines: list[str] = []
            if connected:
                lines.append("Internet: connected. Gentoo can be downloaded.")
            elif self.view.network is False:
                lines.append("Internet: NOT connected. The installation downloads Gentoo, so it needs the internet.")
            else:
                lines.append("Internet: could not be checked.")
            nics = self.view.profile.nics
            lines.append("Network adapters: " + ("" if nics else "none were found. Plug in a network cable or a USB network adapter, then check again."))
            lines.extend(f"  {nic_text(nic)}" for nic in nics)
            if self.manifest.network.proxy:
                lines.append(f"Download proxy: {redact_url_userinfo(self.manifest.network.proxy)}")
            if not connected:
                lines.append("For Wi-Fi press n: it opens nmtui, a text menu where you pick the network and type its password.")
            for line in lines:
                c.para(line, hanging=2)
            options = [Option("Continue")] if connected else [
                Option("Check again"),
                Option("Continue without the internet", detail="The installation will stop at its first download."),
            ]
            commands = {"n": "connect to Wi-Fi (nmtui)", "p": "set a download proxy"}
            if connected:
                commands["r"] = "check again"
            choice = c.menu(options, default=0, commands=commands, help="The installer needs the internet to download Gentoo and its packages. Use a network cable, or press n to connect to Wi-Fi. Behind a company proxy, press p.")
            if choice.command == "n":
                self._run_nmtui()
                self.refresh_view()
            elif choice.command == "p":
                self._ask_proxy()
                self.refresh_view()
            elif choice.command == "r" or (not connected and choice.index == 0):
                self.refresh_view()
            else:
                break
        self._ask_import_profiles()

    def _run_nmtui(self) -> None:
        status = self.host.run_interactive(["nmtui"])
        if status == 127:
            self.console.note("nmtui is not installed here. In another terminal you can use: nmcli device wifi connect NAME password PASSWORD")

    def _ask_proxy(self) -> None:
        network = self.manifest.network
        current = network.proxy
        self.console.para("A download proxy looks like http://proxy.example:3128. Type none if the network needs no proxy"
                          + (f", or keep to leave {redact_url_userinfo(current)} as it is." if current else "."))
        value = self.console.ask(
            "Download proxy", default="keep" if current else "none",
            validate=lambda text: "" if text.lower() in {"none", "-", "keep"} else first_problem(field_errors("network", lambda m: setattr(m.network, "proxy", text))),
        )
        if value.lower() != "keep":
            network.proxy = "" if value.lower() in {"none", "-"} else value

    def _ask_import_profiles(self) -> None:
        profiles = self.view.profile.network_profiles
        network = self.manifest.network
        if not profiles or network.manager != "networkmanager":
            return
        self.console.para(f"This live session has saved network connections ({', '.join(profiles[:4])}{', ...' if len(profiles) > 4 else ''}). They can be copied to the installed system, "
                          "so that it connects by itself the first time it starts. Wi-Fi passwords are copied too; only root can read them there.")
        network.import_live_profiles = self.console.confirm("Copy them to the installed system?", default=network.import_live_profiles)

    # --- 3. this computer ---------------------------------------------------------------------------------

    def step_hardware(self) -> None:
        c = self.console
        while True:
            c.show_lines(self._hardware_lines())
            commands = {"r": "check again"}
            if self._clock_finding() is not None and (self.can_ask_daemon_for("sync_time") or self.can_ask_daemon_for("set_time")):
                commands["t"] = "fix the clock"
            choice = c.menu([Option("Continue")], default=0, commands=commands)
            if choice.command == "t":
                self._fix_clock()
                self.refresh_view()
            elif choice.command == "r":
                self.refresh_view()
            else:
                return

    def _hardware_lines(self) -> list[str]:
        c = self.console
        view = self.view
        lines: list[str] = []
        if view.hardware:
            lines.extend(c.wrap(view.profile.summary(), hanging=2))
            firmware = view.profile.firmware
            boot = "Legacy BIOS" if firmware.mode == "bios" else f"UEFI ({firmware.efi_bits or 64}-bit)"
            secure = "" if firmware.secure_boot in {"n/a", "unknown", "unsupported", ""} else f", Secure Boot {firmware.secure_boot}"
            lines.extend(c.kv_lines("Boot mode", boot + secure))
        else:
            lines.append("No hardware details are available. The installer will still check the important things before it starts.")
        if view.roots:
            usable = sum(1 for entry in view.disk_entries() if entry.usable)
            lines.extend(c.kv_lines("Disks", f"{usable} can be installed to"))
        if view.findings:
            lines.append("")
            lines.append(c.style("What the installer noticed:", "bold"))
            for finding in view.findings:
                lines.extend(self._finding_lines(finding))
        return lines

    def _finding_lines(self, finding: Finding) -> list[str]:
        label, styles = {"error": ("Error: ", ("red", "bold")), "warning": ("Warning: ", ("yellow",)), "info": ("Note: ", ())}[finding.severity]
        return self.console.wrap_labelled(label, finding.message, styles=styles, indent=2)

    def _clock_finding(self) -> Finding | None:
        return next((f for f in self.view.findings if f.action in {"sync_time", "set_time"} or "clock" in f.code), None)

    def _fix_clock(self) -> None:
        c = self.console
        try:
            if self.can_ask_daemon_for("sync_time") and self.view.network is not False:
                c.say("Setting the clock from the internet...")
                ok, message = self.daemon.action("sync_time")
                c.say(message or ("The clock was set." if ok else "That did not work."))
                if ok:
                    return
            if self.can_ask_daemon_for("set_time"):
                c.para("Type the current date and time in UTC (world time, not your local time), for example 2026-10-24 14:30:00")
                text = c.ask(
                    "UTC date and time (YYYY-MM-DD HH:MM:SS)",
                    validate=lambda value: "" if hardware.parse_set_time(value) else "Use the form YYYY-MM-DD HH:MM:SS with a year from 2020 to 2100.",
                    back=False,
                )
                ok, message = self.daemon.action("set_time", utc=text)
                c.say(message or ("The clock was set." if ok else "That did not work."))
        except (OSError, DaemonError) as exc:
            c.warn(f"The installer service could not change the clock: {exc}")

    # --- 4. disk ---------------------------------------------------------------------------------------------------

    def keeps_own_partitions(self) -> bool:
        """A settings file can describe your own partitions (manual mode). This program cannot create such a layout, so it must not touch it."""
        if self.manifest.storage.mode != "manual":
            return False
        self.console.para("These settings use your own partitions (manual mode), which this text installer cannot set up or change. "
                          "The disk and partition choices stay exactly as the settings file says; use the graphical installer to change them.")
        return True

    def step_disk(self) -> None:
        if self.keeps_own_partitions():
            return
        self.substeps(self._choose_disk, self._approve_disk, self._ask_portable)

    def _choose_disk(self) -> bool:
        c = self.console
        storage = self.manifest.storage
        while True:
            entries = self.view.disk_entries()
            usable = [entry for entry in entries if entry.usable]
            for entry in entries:
                if not entry.usable:
                    c.note(f"{entry.disk.path} ({entry.disk.model or 'disk'}) is not offered: it {entry.problem}.")
            if not usable:
                c.error("No disk can be installed to. Check that the disk is connected. On some laptops the disk is hidden by a storage mode (RAID/Intel RST/VMD) in the firmware settings.")
                if c.menu([Option("Scan for disks again")], default=0, help="Restart the computer and look for a storage setting in the firmware (BIOS/UEFI) menu.").index == 0:
                    self.refresh_view()
                continue
            wanted = next((i for i, entry in enumerate(usable) if entry.disk.path == storage.device), None)
            c.para("Which disk should Gentoo be installed on? Everything on it will be erased. Other disks are not touched.")
            choice = c.menu(
                [disk_option(entry) for entry in usable], default=wanted, commands={"r": "scan for disks again"},
                help="Pick the disk by its number. The model, size and serial number should match the disk you mean. If you are not sure, answer q, then check with: lsblk -o NAME,MODEL,SIZE,SERIAL",
            )
            if choice.command == "r":
                self.refresh_view()
                continue
            entry = usable[choice.index]
            if entry.disk.mounted:
                mounts = sorted({m for node in entry.disk.walk() for m in node.mountpoints})
                c.warn(f"{entry.disk.path} is in use ({', '.join(mounts)}). Unmount it first, for example with: umount -R <mount point>; swapoff -a. Then choose r to scan again.")
                continue
            self.entry = entry
            return True

    def _approve_disk(self) -> bool:
        c = self.console
        entry = self.entry
        assert entry is not None
        disk = entry.disk
        if entry.has_identity:
            apply_disk(self.manifest.storage, entry)
            return False
        if entry.shared_identity:
            reason = f"{disk.path} reports a serial number that another disk reports too (some USB adapters do), so the serial number cannot tell them apart."
        else:
            reason = f"{disk.path} reports neither a serial number nor a WWN (a virtual disk or a USB adapter, for example)."
        if not entry.by_path:
            c.error(f"{disk.path} cannot be told apart from other disks by a serial number, and has no stable hardware path either, so the installer cannot make sure it is the same disk later. {NO_IDENTITY_HINT}"
                    if not entry.shared_identity else f"{reason} It has no stable hardware path either, so the installer cannot make sure it is the same disk later. Disconnect the other disk and try again.")
            raise GoBack
        c.para(
            f"{reason} It will be recognised by its hardware path {entry.by_path}, its size ({disk.size} bytes) and its model, "
            "and the installer refuses to continue if any of them changes. Check that this is really the disk you want."
        )
        if not c.confirm("Is this the disk to install on?", default=False, detail=f"{disk.path}: {disk.model or 'unnamed disk'}, {human_size(disk.size)}"):
            raise GoBack
        apply_disk(self.manifest.storage, entry, approve_path=True)
        return True

    def _ask_portable(self) -> bool:
        entry, hw = self.entry, self.manifest.hardware
        assert entry is not None
        disk = entry.disk
        if not (disk.removable or disk.hotplug or disk.transport.lower() == "usb") or hw.target == "portable":
            return False
        self.console.para("This disk is removable (a USB stick or a card). If you will start the installed system on other computers too, choose a portable installation: "
                          "it is not tuned to this computer's processor, has drivers and firmware for more hardware and adds a fallback boot entry. It takes longer and needs more space.")
        hw.target = "portable" if self.console.confirm("Will you start this system on other computers?", default=False) else "this"
        return True

    # --- 5. filesystem and encryption --------------------------------------------------------------------------------

    def step_storage(self) -> None:
        if self.keeps_own_partitions():
            return
        self.substeps(self._ask_filesystem, self._ask_encryption, self._ask_passphrase)

    def _ask_filesystem(self) -> bool:
        c = self.console
        storage = self.manifest.storage
        c.para("This installer sets up the whole disk by itself (guided mode): a boot partition, swap space and one system partition. "
               "To keep other systems on the disk or to choose your own partitions, use the graphical installer, or write a settings file and run: gentoo-installer-text --manifest FILE")
        current = next((i for i, item in enumerate(FILESYSTEMS) if item[0] == storage.filesystem), 0)
        choice = c.menu([Option(label, detail) for _, label, detail in FILESYSTEMS], title="Which filesystem should the system use?", default=current,
                        help="ext4 is the safe choice. XFS and Btrfs are fine if you know why you want them.")
        storage.mode, storage.filesystem = "erase", FILESYSTEMS[choice.index][0]
        if storage.swap_mib:
            c.note(f"Swap space of {storage.swap_mib} MiB is set up, chosen from the amount of memory in this computer.")
        return True

    def _ask_encryption(self) -> bool:
        storage = self.manifest.storage
        storage.luks = self.console.confirm(
            "Encrypt the system disk with a passphrase?", default=storage.luks,
            help="Encryption protects your files if the computer is lost or stolen. You type a passphrase every time the computer starts. If you forget it, the files cannot be recovered.",
        )
        if not storage.luks:
            storage.luks_passphrase, storage.lvm = "", False
        return True

    def _ask_passphrase(self) -> bool:
        c = self.console
        storage, system = self.manifest.storage, self.manifest.system
        if not storage.luks:
            return False
        if storage.luks_passphrase and c.confirm("Keep the passphrase you typed before?", default=True):
            return True
        c.para("You will type this passphrase every time the computer starts, before the system loads. If you forget it, the files cannot be recovered.")
        if system.keymap == "us" and system.keyboard_layout != "us":
            c.warn(f"At start-up the keyboard works like a US keyboard, because the start-up screen cannot show the letters of the {keyboard_label(self.manifest)} layout. "
                   "Type the passphrase with the Latin letters printed on the keys, and write it down.")
        else:
            c.warn(f"At start-up the keyboard layout is the one you chose in step 1: {keyboard_label(self.manifest)} (console keymap {system.keymap}). "
                   "Letters and digits are safest; characters such as ; ' [ ] sit in different places on different layouts.")
        while True:
            first = c.secret("Passphrase", hint="Use at least 8 characters.")

            def change(m: InstallManifest, text: str = first) -> None:
                m.storage.luks, m.storage.luks_passphrase = True, text

            problem = first_problem(field_errors("storage", change))
            if problem:
                c.warn(problem)
                continue
            try:
                again = c.secret("Type it again")
            except GoBack:
                continue
            if again != first:
                c.warn("The two passphrases are not the same. Try again.")
                continue
            storage.luks_passphrase = first
            return True

    # --- 6. desktop and init -----------------------------------------------------------------------------------------

    def step_system(self) -> None:
        self.substeps(self._ask_desktop, self._ask_init)

    def _desktop_problem(self, desktop: str) -> str:
        if desktop == "kde" and any(word in self.manifest.system.stage_variant for word in ("nomultilib", "hardened")):
            return ("KDE Plasma needs a Standard or Desktop stage3: the Hardened and No-multilib profiles do not enable the desktop USE flags Plasma requires, "
                    "so the package step would fail after your disk was erased. Choose another desktop, or the graphical installer for another stage3.")
        entry = self.current_disk()
        if entry is None:
            return ""
        trial = copy.deepcopy(self.manifest)
        trial.system.desktop = desktop
        errors, _ = layout.capacity_problems(trial, entry.disk.size, entry.disk.log_sec or 512)
        return first_problem(errors)

    def _recommended_desktop(self) -> str:
        try:
            for item in recommend.recommend(self.view.hardware, self.manifest):
                if item.field == "system.desktop" and item.suggestion:
                    return item.suggestion
        except Exception:  # noqa: BLE001 - advice only
            pass
        return self.manifest.system.desktop

    def _ask_desktop(self) -> bool:
        c = self.console
        system, options = self.manifest.system, self.manifest.options
        options_list = []
        for key, label, detail in DESKTOPS:
            need = layout.MIN_ROOT_MIB[key] // 1024
            text = f"{detail} Needs about {need} GiB of disk space."
            if self._desktop_problem(key):
                text += " Not possible on the disk you chose."
            options_list.append(Option(label, text))
        keys = [item[0] for item in DESKTOPS]
        names = {key: label for key, label, _ in DESKTOPS}
        suggested = self._recommended_desktop()
        if suggested != system.desktop and suggested in keys:
            c.note(f"This computer has little memory, so {names[suggested]} is suggested.")
        default = keys.index(suggested) if suggested in keys else 0
        while True:
            choice = c.menu(options_list, title="Which desktop do you want?", default=default,
                            help="The desktop is the graphical environment with windows and menus. Choose Minimal for a server or if you only need a command line.")
            problem = self._desktop_problem(keys[choice.index])
            if problem:
                c.warn(problem)
                continue
            break
        was_minimal = system.desktop == "minimal"
        system.desktop = keys[choice.index]
        if system.desktop == "minimal":
            options.install_hwmonux = options.install_portagehub = False  # graphical programs
        elif was_minimal:
            options.install_hwmonux = options.install_portagehub = True
        return True

    def _ask_init(self) -> bool:
        system = self.manifest.system
        current = next((i for i, item in enumerate(INITS) if item[0] == system.init), 0)
        choice = self.console.menu([Option(label, detail) for _, label, detail in INITS], title="Which init system do you want? (It starts and stops the programs on the computer.)",
                                   default=current, help="If you do not know, take OpenRC: it is the Gentoo default.")
        system.init = INITS[choice.index][0]
        # The stage3 archive follows the init system and keeps its family (a settings file may name a hardened or no-multilib one);
        # with an empty profile the installer derives the matching Portage profile.
        family = system.stage_variant.rsplit("-", 1)[0]
        family = family if family in STAGE_FAMILIES else "amd64"
        system.stage_variant, system.profile, system.multilib = f"{family}-{system.init}", "", "nomultilib" not in family
        return True

    # --- 7. accounts ------------------------------------------------------------------------------------------------------

    def step_accounts(self) -> None:
        self.substeps(self._ask_hostname, self._ask_username, self._ask_user_password, self._ask_root_mode, self._ask_root_password, self._apply_root_mode)

    def _ask_hostname(self) -> bool:
        accounts = self.manifest.accounts
        accounts.hostname = self.console.ask(
            "Computer name (hostname)", default=accounts.hostname, help="The name of this computer on the network. Letters, digits and hyphens only.",
            validate=lambda value: first_problem(field_errors("accounts", lambda m: setattr(m.accounts, "hostname", value))),
        )
        return True

    def _ask_username(self) -> bool:
        accounts = self.manifest.accounts

        def check(value: str) -> str:
            if value in RESERVED_USERNAMES:
                return f"{value} is a name the system uses itself. Choose another user name."
            return first_problem(field_errors("accounts", lambda m: setattr(m.accounts, "username", value)))

        accounts.username = self.console.ask("Your user name", default=accounts.username, validate=check,
                                             help="The name you log in with. Lowercase letters, digits, - and _ ; it must start with a letter.")
        return True

    def _ask_password(self, prompt: str, current: str, label: str) -> str:
        c = self.console
        if current and c.confirm(f"Keep the {label} you typed before?", default=True):
            return current
        while True:
            first = c.secret(prompt, hint="Use at least 8 characters.")

            def change(m: InstallManifest, text: str = first) -> None:
                m.accounts.password = m.accounts.root_password = text

            problem = first_problem(field_errors("accounts", change))
            if problem:
                c.warn(problem)
                continue
            try:
                again = c.secret("Type it again")
            except GoBack:
                continue
            if again != first:
                c.warn("The two passwords are not the same. Try again.")
                continue
            return first

    def _ask_user_password(self) -> bool:
        accounts = self.manifest.accounts
        accounts.password = self._ask_password(f"Password for {accounts.username}", accounts.password, "password")
        return True

    def _ask_root_mode(self) -> bool:
        accounts = self.manifest.accounts
        modes = ("same", "separate", "off")
        if accounts.disable_root_login:
            self.root_mode = "off"
        elif accounts.root_password and accounts.root_password != accounts.password:
            self.root_mode = "separate"
        choice = self.console.menu(
            [
                Option("Use the same password for the administrator (root) account"),
                Option("Choose a separate password for root"),
                Option("Turn the root login off", detail=f"You administer the system with sudo from the account {accounts.username}."),
            ],
            title="How should the administrator (root) account work?", default=modes.index(self.root_mode),
            help="root is the all-powerful administrator account. With the third choice nobody can log in as root directly; use sudo.",
        )
        self.root_mode = modes[choice.index]
        return True

    def _ask_root_password(self) -> bool:
        accounts = self.manifest.accounts
        if self.root_mode != "separate":
            return False
        accounts.root_password = self._ask_password("Password for root", accounts.root_password if accounts.root_password != accounts.password else "", "root password")
        return True

    def _apply_root_mode(self) -> bool:
        accounts = self.manifest.accounts
        accounts.disable_root_login = self.root_mode == "off"
        if self.root_mode == "same":
            accounts.root_password = accounts.password
        elif self.root_mode == "off":
            accounts.root_password = ""
        return False

    # --- 8. time zone and language ----------------------------------------------------------------------------------------

    def step_region(self) -> None:
        self.substeps(self._ask_timezone, self._ask_locale)

    def _ask_timezone(self) -> bool:
        c = self.console
        system = self.manifest.system
        zones = self.host.timezones()
        if not zones:
            c.para("Type the name of your time zone, for example Europe/Berlin or UTC.")
            system.timezone = c.ask("Time zone", default=system.timezone,
                                    validate=lambda value: first_problem(field_errors("locale", lambda m: setattr(m.system, "timezone", value))))
            return True
        regions = [region for region in TIMEZONE_REGIONS if any(zone.startswith(region + "/") for zone in zones)]
        labels = ["UTC (world time, no region)", *regions, "Search all time zones"]
        current_region = system.timezone.split("/")[0]
        default = regions.index(current_region) + 1 if current_region in regions else 0
        while True:
            c.para("Where are you? This sets the time zone of the installed system.")
            index = c.pick(labels, default=default, help="Pick your continent first, then your city or region. The last entry searches every zone by name.")
            if index == 0:
                system.timezone = "UTC"
                return True
            if index == len(labels) - 1:
                names, prefix = zones, ""
            else:
                prefix = regions[index - 1] + "/"
                names = [zone for zone in zones if zone.startswith(prefix)]
            shown = [zone[len(prefix):] for zone in names]
            wanted = names.index(system.timezone) if system.timezone in names else None
            try:
                chosen = c.pick(shown, title="Choose your time zone:", default=wanted, help="Type part of a city name to narrow the list, for example berlin.")
            except GoBack:
                continue
            system.timezone = names[chosen]
            return True

    def _ask_locale(self) -> bool:
        c = self.console
        system = self.manifest.system
        locales = self.host.locales()
        wanted = system.locale
        hint = LOCALE_FOR_LAYOUT.get(system.keyboard_layout.split(",")[0])
        if wanted == "en_US.UTF-8" and hint in locales:
            wanted = hint or wanted
        default = locales.index(wanted) if wanted in locales else 0
        c.para("Which language and regional formats (dates, numbers) should the installed system use?")
        index = c.pick(locales, default=default, help="For example en_US.UTF-8 is English as used in the USA and de_DE.UTF-8 is German. Type part of a name to search, for example de_.")
        problem = first_problem(field_errors("locale", lambda m: setattr(m.system, "locale", locales[index])))
        if problem:
            c.warn(problem)
            raise GoBack
        system.locale = locales[index]
        return True

    # --- the review -----------------------------------------------------------------------------------------------------------------

    def review(self) -> StartPlan | Jump | Refused:
        c = self.console
        manifest = self.manifest
        storage = manifest.storage
        device = storage.device
        self.fill_missing_secrets()
        self.ensure_identity()
        wrong = 0
        resume = False
        if self.resume_requested and not self._resumable():
            c.warn("There is no interrupted installation to resume, so this would be a new installation that erases the disk.")
        elif self.resume_requested:
            resume = True
        elif self._resumable():
            resume = c.confirm("Resume it?", default=False, detail="An earlier installation stopped after the disk was prepared. Resuming continues it without erasing the disk again.")
        help_text = "c = change a setting, r = check this computer again, b = back, q = quit"
        summary = build_summary(manifest, self.view, require_identity=True)
        render_summary(c, summary)
        confirmed: list[str] = []
        while True:
            if summary.problems:
                c.para("Fix the problems above (c = change a setting, r = check this computer again), or quit.")
                text = c.ask("Your choice", allow_empty=True, help=help_text)
            else:
                if storage.mode == "manual":
                    what = "continue the installation (nothing is formatted again)" if resume else "install Gentoo on the partitions listed above"
                    c.para(f"To {what}, type the name of the main disk exactly as it is written here: {device}")
                else:
                    what = f"continue the installation on {device} (nothing is erased again)" if resume else f"erase {device} and install Gentoo on it"
                    c.para(f"To {what}, type the disk name exactly as it is written here: {device}")
                text = c.ask("Type the disk name to start", allow_empty=True, help=help_text)
            word = text.strip().lower()
            if text.strip() == device:
                if summary.problems:
                    c.warn("The problems above must be fixed first.")
                    continue
                if self._confirm_other_disks(confirmed):
                    return StartPlan(resume=resume)
                return Refused("The name of another disk that the layout changes was typed wrongly.")
            if word in {"b", "back"}:
                raise GoBack
            if word in {"q", "quit"}:
                if c.confirm_quit():
                    raise Quit
                continue
            if word == "r":
                self.refresh_view()
                summary = build_summary(manifest, self.view, require_identity=True)
                render_summary(c, summary)
                continue
            if word == "c":
                jump = self._change_menu()
                if jump is not None:
                    return jump
                continue
            if not word:
                continue
            wrong += 1
            c.warn(f"That is not {device}. Nothing has been changed.")
            if wrong >= MAX_WRONG_CONFIRMATIONS:
                return Refused("The disk name was typed wrongly too often.")

    def required_disks(self) -> list[str]:
        """Every disk the installation changes: the target, and for your own partitions the disk of each partition."""
        storage = self.manifest.storage
        wanted = [storage.device]
        if storage.mode == "manual":
            for row in storage.partitions:
                try:
                    node = blockdev.find(self.view.roots, row.device)
                except (LookupError, ValueError):
                    continue
                wanted.extend(disk.path for disk in blockdev.parent_disks(self.view.roots, node) if disk.path not in wanted)
        return wanted

    def _confirm_other_disks(self, confirmed: list[str]) -> bool:
        """The first disk was typed; a layout on several disks needs every one of them typed (like the graphical installer)."""
        for path in self.required_disks()[1:]:
            if path in confirmed:
                continue
            self.console.para(f"This layout also changes {path}. Type its name to confirm.")
            for _ in range(MAX_WRONG_CONFIRMATIONS):
                if self.console.ask(f"Type {path}", allow_empty=True).strip() == path:
                    confirmed.append(path)
                    break
                self.console.warn(f"That is not {path}. Nothing has been changed.")
            else:
                return False
        return True

    def _resumable(self) -> bool:
        status = self.state.status if self.state is not None else {}
        return bool(status.get("resumable")) and status.get("status") != "running"

    def _change_menu(self) -> Jump | None:
        choice = self.console.menu([Option(title) for title, _ in self.steps], title="Which setting do you want to change? You will go through the steps from there; Enter keeps your earlier answers.", back=True)
        return Jump(choice.index)

    def ensure_identity(self) -> None:
        """A settings file written by hand does not say which physical disk it means: pin it, as the disk step does."""
        storage = self.manifest.storage
        if storage.device_id or not storage.device:
            return
        entry = self.current_disk()
        if entry is None or not entry.usable:
            return  # the summary reports it
        self.entry = entry
        try:
            self._approve_disk()
        except GoBack:
            return  # not approved: the summary says the disk is not identified
        if entry.has_identity:
            self.console.note(f"{storage.device} is recognised by its {'WWN' if entry.disk.wwn else 'serial number'}, so the installer refuses to continue if another disk shows up under that name.")

    def fill_missing_secrets(self) -> None:
        """A settings file has no passwords (they are never saved): ask for the ones that are missing."""
        c = self.console
        accounts, storage = self.manifest.accounts, self.manifest.storage
        if storage.luks and not storage.luks_passphrase:
            c.para("The settings file does not contain the disk passphrase (passwords are never saved).")
            self._ask_passphrase()
        if not accounts.password:
            c.para("The settings file does not contain passwords (they are never saved).")
            self._ask_user_password()
        if not accounts.disable_root_login and not accounts.root_password:
            if c.confirm("Use the same password for root?", default=True, detail=f"The settings file has no root password. It can be the same as the password of {accounts.username}."):
                self.root_mode = "same"
            else:
                self.root_mode = "separate"
                self._ask_root_password()
            self._apply_root_mode()
