"""PORT: the Portage tab of the Customize page (mirror, binary host, build jobs, make.conf variables and USE flags)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget

from .. import system_info
from ..widgets import Card
from .hardware import hardware_plan

if TYPE_CHECKING:
    from ..manifest import InstallManifest
    from .hardware import HardwareTab

AUTO_PLACEHOLDER = "Leave empty: chosen for this computer"


def auto_jobs() -> int:
    """Parallel build jobs this computer can sustain: one per processor, but
    not more than one per 2 GiB of memory (big C++ and Rust builds)."""
    cpus = os.cpu_count() or 2
    return max(1, min(cpus, int(system_info._memory_gib() // 2) or 1))


def auto_make_opts() -> str:
    return f"-j{auto_jobs()} -l{os.cpu_count() or 2}"


def parse_jobs(make_opts: str) -> int | None:
    try:
        return int(make_opts.strip().removeprefix("-j").split()[0])
    except (ValueError, IndexError):
        return None



class PortageTabMixin:
    """The Portage tab's widgets (mirror, jobs, cflags, USE flags, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab
        _loaded_make_opts: str

        def _update_hardware_note(self, info: system_info.SystemInfo | None, scalars: dict[str, Any]) -> None: ...  # tools_tab

    def _portage_tab(self) -> QWidget:
        page = QWidget()
        card = Card(parent=page)
        layout = QVBoxLayout(page)
        layout.addWidget(card)
        form = QFormLayout()
        self.mirror = QLineEdit()
        form.addRow("HTTPS download mirror", self.mirror)
        self.binhost = QCheckBox("Prefer official Gentoo binary packages when available")
        form.addRow(self.binhost)
        self.binhost_url = QLineEdit()
        form.addRow("Binary package host", self.binhost_url)
        jobs_row = QHBoxLayout()
        self.jobs = QSpinBox()
        self.jobs.setRange(1, 256)
        self.jobs.setValue(auto_jobs())
        self.jobs_auto = QCheckBox()
        self.jobs_auto.setText(f"Automatic ({auto_jobs()} jobs for {os.cpu_count() or 2} processors)")
        self.jobs_auto.setToolTip("Chosen from this computer's processors and memory so that builds neither crawl nor run out of memory.")
        self.jobs_auto.toggled.connect(lambda checked: self.jobs.setEnabled(not checked))
        jobs_row.addWidget(self.jobs_auto)
        jobs_row.addWidget(self.jobs)
        jobs_row.addStretch()
        form.addRow("Parallel build jobs", jobs_row)
        self.cflags = QLineEdit()
        self.cflags.setToolTip("Used for C, C++ and Fortran. -march=native can optimize for this computer, but the programs may not run on a different CPU.")
        form.addRow("COMMON_FLAGS", self.cflags)
        self.cxxflags = QLineEdit()
        self.cxxflags.setToolTip("Normally ${CFLAGS}, which follows COMMON_FLAGS.")
        form.addRow("CXXFLAGS", self.cxxflags)
        self.rustflags = QLineEdit()
        form.addRow("RUSTFLAGS", self.rustflags)
        self.emerge_opts = QLineEdit()
        form.addRow("EMERGE_DEFAULT_OPTS", self.emerge_opts)
        self.features = QLineEdit()
        self.features.setPlaceholderText("ccache getbinpkg (space separated)")
        form.addRow("FEATURES", self.features)
        self.licenses = QLineEdit()
        form.addRow("ACCEPT_LICENSE", self.licenses)
        self.use_flags = QPlainTextEdit()
        self.use_flags.setPlaceholderText("One global USE flag per line; prefix with - to disable")
        self.use_flags.setMaximumHeight(85)
        self.use_flags.setTabChangesFocus(True)
        form.addRow("Global USE flags", self.use_flags)
        self.package_use = QPlainTextEdit()
        self.package_use.setPlaceholderText("category/package flag -flag, one entry per line")
        self.package_use.setMaximumHeight(85)
        self.package_use.setTabChangesFocus(True)
        form.addRow("Per-package USE flags", self.package_use)
        self.cpu_flags = QLineEdit()
        self.cpu_flags.setPlaceholderText(AUTO_PLACEHOLDER)
        form.addRow("CPU_FLAGS_X86", self.cpu_flags)
        self.video_cards = QLineEdit()
        self.video_cards.setPlaceholderText(AUTO_PLACEHOLDER)
        form.addRow("VIDEO_CARDS", self.video_cards)
        card.content.addLayout(form)
        layout.addStretch(1)
        return page

    def refresh_placeholders(self) -> None:
        """'Leave empty: chosen for this computer (now: ...)': say what an empty field will become."""
        info = self.hardware.info
        make_conf: dict[str, str] = {}
        plan_scalars: dict[str, Any] = {}
        if info is not None and info.hardware:
            plan = hardware_plan(self.hardware.working_manifest(), info.hardware)
            make_conf = dict(plan.make_conf) if plan is not None else {}
            plan_scalars = dict(plan.scalars) if plan is not None else {}
        self._update_hardware_note(info, plan_scalars)
        for edit, key in ((self.cpu_flags, "CPU_FLAGS_X86"), (self.video_cards, "VIDEO_CARDS")):
            value = make_conf.get(key, "").replace("${VIDEO_CARDS}", "profile default +")
            if info is None or not info.hardware:
                edit.setPlaceholderText(AUTO_PLACEHOLDER)
            elif value:
                shown = value if len(value) <= 48 else value[:45] + "..."
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (now: {shown})")
            else:
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (the profile default)")

    def load_portage(self) -> None:
        portage = self.manifest.portage
        self.mirror.setText(portage.mirror)
        self.binhost.setChecked(portage.binhost)
        self.binhost_url.setText(portage.binhost_url)
        self._loaded_make_opts = portage.make_opts.strip()
        explicit = parse_jobs(self._loaded_make_opts) if self._loaded_make_opts and self._loaded_make_opts != auto_make_opts() else None
        self.jobs_auto.setChecked(explicit is None)
        self.jobs.setValue(explicit if explicit is not None else auto_jobs())
        self.jobs.setEnabled(explicit is not None)
        self.cflags.setText(portage.cflags)
        self.cxxflags.setText(portage.cxxflags)
        self.rustflags.setText(portage.rustflags)
        self.emerge_opts.setText(portage.emerge_opts)
        self.features.setText(" ".join(portage.features))
        self.licenses.setText(portage.accept_license)
        self.use_flags.setPlainText("\n".join(portage.use_flags))
        self.package_use.setPlainText("\n".join(f"{atom} {' '.join(flags)}" for atom, flags in portage.package_use.items()))
        self.cpu_flags.setText(portage.cpu_flags)
        self.video_cards.setText(portage.video_cards)

    def save_portage(self) -> None:
        portage = self.manifest.portage
        portage.mirror = self.mirror.text().strip().rstrip("/")
        portage.binhost = self.binhost.isChecked()
        portage.binhost_url = self.binhost_url.text().strip()
        if self.jobs_auto.isChecked():
            portage.make_opts = ""  # the installer sizes the jobs from this computer's processors, memory and swap
        elif self._loaded_make_opts and parse_jobs(self._loaded_make_opts) == self.jobs.value():
            portage.make_opts = self._loaded_make_opts  # untouched: keep extra flags such as -l
        else:
            portage.make_opts = f"-j{self.jobs.value()}"
        portage.cflags = self.cflags.text().strip()
        portage.cxxflags = self.cxxflags.text().strip()
        portage.rustflags = self.rustflags.text().strip()
        portage.emerge_opts = self.emerge_opts.text().strip()
        portage.features = self.features.text().split()
        portage.accept_license = self.licenses.text().strip()
        portage.use_flags = self.use_flags.toPlainText().split()
        portage.package_use = {}
        for line in self.package_use.toPlainText().splitlines():
            words = line.split()
            if words:
                portage.package_use[words[0]] = words[1:]
        portage.cpu_flags = self.cpu_flags.text().strip()
        portage.video_cards = self.video_cards.text().strip()
