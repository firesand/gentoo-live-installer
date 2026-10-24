"""PORT: the Portage tab of the Customize page.

Guided mode shows the few choices a newcomer needs, each with a plain sentence: how the compiler is tuned, whether ready-made
packages are used, where programs are downloaded from and which licences are accepted. Everything else (build jobs, flags,
USE flags, the package tree, graphics drivers, the world update) is under "More options", grouped as Speed, Packages,
Licences and Advanced. Switching between Guided and Advanced only hides or shows the controls; nothing is cleared.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import binhost as binhost_rules
from .. import licenses, makeopts, mirrors, optimize, system_info, usedesc, world
from ..widgets import Card, FlowLayout, wrapped_label
from .hardware import hardware_plan
from .help import AdvancedSection, Badge, HelpNote

if TYPE_CHECKING:
    from ..manifest import InstallManifest
    from .hardware import HardwareTab

AUTO_PLACEHOLDER = "Leave empty: chosen for this computer"
MAKE_CONF_HELP = "make.conf is the main file that tells Portage how to build programs."
BINHOST_HELP = ("Gentoo offers pre-built packages so you do not have to compile everything; the result is the same system and it saves hours. "
                "Every package is checked against Gentoo's signature. The NVIDIA driver is never among them: it is always built on your computer.")
SOURCES_HELP = "Programs are downloaded from a server; choosing one near you makes the install faster."
JOBS_HELP = "Each build job needs about 2 GiB of memory; too many jobs make the computer run out of memory and the build fail."
POPULAR_FLAGS = ("X", "wayland", "kde", "gtk", "qt6", "pulseaudio", "pipewire", "bluetooth", "cups", "dvd", "alsa", "vulkan", "opengl", "networkmanager",
                 "systemd", "ssl", "unicode", "wifi", "ipv6", "zeroconf")
USE_STATE_LABELS = (("Default", "default"), ("On", "on"), ("Off", "off"))
HANDBOOK_BINHOST = "Installation/Base#Optional:_Adding_a_binary_package_host"
HANDBOOK_LICENSE = "Installation/Base#Optional:_Configure_the_ACCEPT_LICENSE_variable"
HANDBOOK_SYNC = "Installation/Base#Optional:_Updating_the_Gentoo_ebuild_repository"
HANDBOOK_WORLD = "Installation/Base#Optional:_Updating_the_.40world_set"
HANDBOOK_VIDEO = "Installation/Base#VIDEO_CARDS"


def auto_jobs(swap_mib: int = 0) -> int:
    """Parallel build jobs this computer can sustain: one per processor, but not more than one per 2 GiB of memory (big C++ and Rust builds)."""
    return makeopts.auto_jobs(os.cpu_count() or 2, int(system_info._memory_gib() * 1024**3), swap_mib)


def auto_make_opts(swap_mib: int = 0) -> str:
    """The MAKEOPTS the installer chooses for this computer: the same function the daemon uses."""
    return makeopts.auto_make_opts(os.cpu_count() or 2, int(system_info._memory_gib() * 1024**3), swap_mib)


def parse_jobs(make_opts: str) -> int | None:
    return makeopts.parse_make_opts(make_opts)[0]


class _ChoiceLabel(QLabel):
    """The wrapped text of a radio button: a radio button's own text cannot wrap, which would push a narrow window wider."""

    def __init__(self, text: str, radio: QRadioButton) -> None:
        super().__init__(text)
        self._radio = radio
        self.setWordWrap(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        if self._radio.isEnabled():
            self._radio.click()
        super().mouseReleaseEvent(event)


def _radio_row(radio: QRadioButton, text: str, badge: QWidget | None = None) -> QHBoxLayout:
    radio.setAccessibleName(text)
    row = QHBoxLayout()
    row.addWidget(radio, 0, Qt.AlignmentFlag.AlignTop)
    row.addWidget(_ChoiceLabel(text, radio), 1)
    if badge is not None:
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
    return row


def _with_help(widget: QWidget, text: str, anchor: str | None = None) -> QWidget:
    """A control with its "i" note on the same line."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 1)
    layout.addWidget(HelpNote(text, anchor), 0, Qt.AlignmentFlag.AlignTop)
    return row


def _group(title: str) -> tuple[QGroupBox, QFormLayout]:
    box = QGroupBox(title)
    form = QFormLayout(box)
    return box, form


class PortageTabMixin:
    """The Portage tab's widgets (mirror, jobs, cflags, USE flags, ...) are attributes of the page that mixes this in."""

    if TYPE_CHECKING:
        manifest: InstallManifest
        hardware: HardwareTab

        def _update_hardware_note(self, info: system_info.SystemInfo | None, scalars: dict[str, Any]) -> None: ...  # tools_tab

    # --- building the tab ----------------------------------------------------------------------------------------

    def _portage_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        card = Card("How programs are installed", MAKE_CONF_HELP, parent=page)
        layout.addWidget(card)
        self._loading_portage = True
        self._build_guided(card)
        self.portage_advanced = AdvancedSection("portage")
        layout.addWidget(self.portage_advanced)
        self._build_speed(self.portage_advanced)
        self._build_packages(self.portage_advanced)
        self._build_licences(self.portage_advanced)
        self._build_advanced(self.portage_advanced)
        layout.addStretch(1)
        self._loading_portage = False
        return page

    def _build_guided(self, card: Card) -> None:
        form = QFormLayout()
        self.optimize = QComboBox()
        self.optimize.addItem("Works on any PC (recommended)", "generic")
        self.optimize.addItem("Tuned for this CPU", "native")
        self.optimize.setAccessibleName("Compiler optimisation")
        self.optimize.currentIndexChanged.connect(lambda _=0: self._optimize_changed())
        form.addRow("Compiler optimisation", _with_help(self.optimize, optimize.TUNING_HELP, "Installation/Stage#CFLAGS_and_CXXFLAGS"))
        self.optimize_note = wrapped_label("", muted=True)
        self.optimize_note.hide()
        form.addRow(self.optimize_note)

        self.binhost = QCheckBox("Download ready-made packages (much faster)")
        self.binhost.setToolTip(BINHOST_HELP)
        form.addRow(_with_help(self.binhost, BINHOST_HELP, HANDBOOK_BINHOST))

        self.source_kind = QComboBox()
        self.source_kind.addItem("Automatic (recommended)", mirrors.SOURCE_AUTOMATIC)
        self.source_kind.addItem("Choose by country", mirrors.SOURCE_COUNTRY)
        self.source_kind.addItem("Custom address", mirrors.SOURCE_CUSTOM)
        self.source_kind.setAccessibleName("Download sources")
        self.source_kind.currentIndexChanged.connect(lambda _=0: self._source_changed())
        form.addRow("Download sources", _with_help(self.source_kind, SOURCES_HELP, "Mirrors"))
        self.source_note = wrapped_label("distfiles.gentoo.org sends you to a server near you.", muted=True)
        form.addRow(self.source_note)
        self.mirror_list = QListWidget()
        self.mirror_list.setAccessibleName("Mirrors, in the order they are tried")
        self.mirror_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.mirror_list.setMinimumHeight(140)
        for mirror in mirrors.MIRRORS:
            self._add_mirror_item(mirror.url, f"{mirrors.COUNTRIES[mirror.country]}: {mirror.name}")
        move = QHBoxLayout()
        self.mirror_up = QPushButton("Move up")
        self.mirror_down = QPushButton("Move down")
        self.mirror_up.clicked.connect(lambda: self._move_mirror(-1))
        self.mirror_down.clicked.connect(lambda: self._move_mirror(1))
        move.addWidget(self.mirror_up)
        move.addWidget(self.mirror_down)
        move.addStretch(1)
        self.mirror_box = QWidget()
        mirror_layout = QVBoxLayout(self.mirror_box)
        mirror_layout.setContentsMargins(0, 0, 0, 0)
        mirror_layout.addWidget(wrapped_label("Tick the servers to use. They are tried from the top.", muted=True))
        mirror_layout.addWidget(self.mirror_list)
        mirror_layout.addLayout(move)
        form.addRow(self.mirror_box)
        self.mirror = QLineEdit()
        self.mirror.setPlaceholderText("https://mirror.example.org/gentoo")
        self.mirror.setAccessibleName("Custom download address")
        self.mirror.setToolTip("Only https:// addresses are accepted, so the downloads are protected.")
        form.addRow("Custom address", self.mirror)
        self.mirror_custom_row = form.labelForField(self.mirror)
        self.proxy_note = wrapped_label("A download proxy is set on the Network tab.", muted=True)
        form.addRow(self.proxy_note)

        self.license_free = QRadioButton()
        self.license_redistributable = QRadioButton()
        self.license_free.setToolTip("Only programs whose source code is open.")
        self.license_redistributable.setToolTip("Recommended for most computers: also allows firmware and drivers that are free to use but not open source.")
        self.license_group = QButtonGroup(self.license_free)
        self.license_group.addButton(self.license_free)
        self.license_group.addButton(self.license_redistributable)
        self.license_free.toggled.connect(lambda checked: checked and self._license_radio("free"))
        self.license_redistributable.toggled.connect(lambda checked: checked and self._license_radio("redistributable"))
        radios = QVBoxLayout()
        radios.addLayout(_radio_row(self.license_free, "Free software only"))
        radios.addLayout(_radio_row(self.license_redistributable, "Free software and redistributable binaries (firmware, drivers)", Badge("Recommended")))
        self.license_custom_note = wrapped_label("Your licence settings are set by hand under More options.", muted=True)
        self.license_custom_note.hide()
        radios.addWidget(self.license_custom_note)
        holder = QWidget()
        holder.setLayout(radios)
        form.addRow("Licences", _with_help(holder, licenses.LICENSE_HELP, HANDBOOK_LICENSE))

        self.jobs_summary = wrapped_label("", muted=True)
        form.addRow("Build jobs", _with_help(self.jobs_summary, JOBS_HELP, "Installation/Stage#MAKEOPTS"))
        self.use_summary = wrapped_label("USE flags are chosen for the desktop you picked. Change them under More options.", muted=True)
        form.addRow("USE flags", _with_help(self.use_summary, usedesc.USE_HELP, "UseFlags"))
        card.content.addLayout(form)

    def _build_speed(self, section: AdvancedSection) -> None:
        box, form = _group("Speed")
        self.jobs_auto = QCheckBox()
        self.jobs_auto.setText(f"Automatic ({auto_jobs()} jobs for {os.cpu_count() or 2} processors)")
        self.jobs_auto.setToolTip("Chosen from this computer's processors and memory so that builds neither crawl nor run out of memory.")
        self.jobs_auto.toggled.connect(lambda checked: self._jobs_auto_toggled(checked))
        self.jobs = QSpinBox()
        self.jobs.setRange(1, 256)
        self.jobs.setValue(auto_jobs())
        self.jobs.setToolTip("MAKEOPTS -j: how many compile jobs run at the same time.")
        self.load_limit = QSpinBox()
        self.load_limit.setRange(0, 512)
        self.load_limit.setSpecialValueText("No limit")
        self.load_limit.setValue(os.cpu_count() or 2)
        self.load_limit.setToolTip("MAKEOPTS -l: no new job starts while the computer is busier than this.")
        form.addRow(self.jobs_auto)
        form.addRow("Parallel build jobs (-j)", self.jobs)
        form.addRow("Load limit (-l)", self.load_limit)
        self.cflags = QLineEdit()
        self.cflags.setToolTip("COMMON_FLAGS: used for C, C++ and Fortran. Tuning for this CPU is chosen above, not here.")
        self.cflags.textChanged.connect(lambda _="": self._flags_changed())
        form.addRow("Compiler flags (COMMON_FLAGS)", self.cflags)
        self.flags_warning = wrapped_label("")
        self.flags_warning.setStyleSheet("color: #b87818;")
        self.flags_warning.hide()
        form.addRow(self.flags_warning)
        self.cxxflags = QLineEdit()  # CXXFLAGS always follows COMMON_FLAGS; the field is kept so a value from a settings file survives
        self.cxxflags.hide()
        self.rustflags = QLineEdit()
        self.rustflags.setToolTip("RUSTFLAGS: for Rust programs. 'Tuned for this CPU' adds -C target-cpu=native.")
        form.addRow("Rust flags (RUSTFLAGS)", self.rustflags)
        self.cpu_flags = QLineEdit()
        self.cpu_flags.setPlaceholderText(AUTO_PLACEHOLDER)
        self.cpu_flags.setToolTip("CPU_FLAGS_X86: the processor features programs may use. 'Tuned for this CPU' writes the detected ones to package.use.")
        form.addRow("Processor features (CPU_FLAGS_X86)", self.cpu_flags)
        section.add_widget(box)
        self.speed_box = box

    def _build_packages(self, section: AdvancedSection) -> None:
        box, form = _group("Packages")
        self.binhost_variant = QComboBox()
        self.binhost_variant.addItem("Automatic (chosen from this processor)", "auto")
        self.binhost_variant.addItem("Generic: runs on every 64-bit PC", "generic")
        self.binhost_variant.addItem("x86-64-v3: needs a processor from about 2015 on", "v3")
        self.binhost_variant.setAccessibleName("Ready-made packages variant")
        form.addRow("Ready-made packages", self.binhost_variant)
        self.binhost_url = QLineEdit()
        self.binhost_url.setToolTip("Where the ready-made packages come from. Their signatures are always checked.")
        form.addRow("Binary package host", self.binhost_url)
        self.sync_snapshot = QRadioButton()
        self.sync_rsync = QRadioButton()
        self.sync_group = QButtonGroup(self.sync_snapshot)
        self.sync_group.addButton(self.sync_snapshot)
        self.sync_group.addButton(self.sync_rsync)
        self.sync_rsync.toggled.connect(lambda _=False: self._sync_changed())
        sync_box = QWidget()
        sync_layout = QVBoxLayout(sync_box)
        sync_layout.setContentsMargins(0, 0, 0, 0)
        sync_layout.addLayout(_radio_row(self.sync_snapshot, "Download a snapshot from the web (works behind firewalls)"))
        sync_layout.addLayout(_radio_row(self.sync_rsync, "Sync with rsync (fresher; needs port 873)"))
        form.addRow("Package tree", _with_help(sync_box, "The package tree lists every program Gentoo can install. A snapshot is checked against Gentoo's signature, "
                                                         "and so is what rsync delivers.", HANDBOOK_SYNC))
        self.rsync_mirror = QLineEdit()
        self.rsync_mirror.setPlaceholderText("rsync://rsync.gentoo.org/gentoo-portage")
        self.rsync_mirror.setAccessibleName("rsync server")
        form.addRow("rsync server", self.rsync_mirror)

        self.use_search = QLineEdit()
        self.use_search.setPlaceholderText("Search USE flags, for example dvd or bluetooth")
        self.use_search.setAccessibleName("Search USE flags")
        self.use_search.textChanged.connect(lambda _="": self._fill_use_table())
        form.addRow(_with_help(self.use_search, usedesc.USE_HELP, "UseFlags"))
        self.use_table = QTableWidget(0, 3)
        self.use_table.setHorizontalHeaderLabels(["Flag", "What it does", "State"])
        self.use_table.setAccessibleName("USE flags")
        self.use_table.verticalHeader().hide()
        self.use_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.use_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.use_table.setMinimumHeight(200)
        self.use_table.setMinimumWidth(0)
        self.use_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.use_table.horizontalHeader().setMinimumSectionSize(40)
        form.addRow(self.use_table)
        custom = QHBoxLayout()
        self.use_custom = QLineEdit()
        self.use_custom.setPlaceholderText("Another flag, for example kde or -gtk")
        self.use_custom.setAccessibleName("Add a custom USE flag")
        self.use_add = QPushButton("Add custom flag")
        self.use_add.clicked.connect(self._add_custom_flag)
        self.use_custom.returnPressed.connect(self._add_custom_flag)
        custom.addWidget(self.use_custom, 1)
        custom.addWidget(self.use_add)
        form.addRow(custom)
        self.use_error = wrapped_label("")
        self.use_error.setStyleSheet("color: #c0392b;")
        self.use_error.hide()
        form.addRow(self.use_error)
        self.use_flags = QPlainTextEdit()
        self.use_flags.setPlaceholderText("One global USE flag per line; prefix with - to disable")
        self.use_flags.setMaximumHeight(85)
        self.use_flags.setTabChangesFocus(True)
        self.use_flags.textChanged.connect(self._use_text_changed)
        form.addRow("Global USE flags (for experts)", self.use_flags)
        self.package_use = QPlainTextEdit()
        self.package_use.setPlaceholderText("category/package flag -flag, one entry per line")
        self.package_use.setMaximumHeight(85)
        self.package_use.setTabChangesFocus(True)
        form.addRow("Per-package USE flags", self.package_use)

        self.video_boxes: dict[str, QCheckBox] = {}
        flow_host = QWidget()
        flow = FlowLayout(flow_host)
        for name, description in usedesc.video_cards().items():
            check = QCheckBox(name)
            check.setToolTip(description)
            check.toggled.connect(lambda _=False: self._video_box_toggled())
            self.video_boxes[name] = check
            flow.addWidget(check)
        form.addRow("Graphics drivers (VIDEO_CARDS)", _with_help(flow_host, "The graphics driver package set for your graphics card; the installer detects it for you.", HANDBOOK_VIDEO))
        self.video_note = wrapped_label("", muted=True)
        form.addRow(self.video_note)
        self.video_cards = QLineEdit()
        self.video_cards.setPlaceholderText(AUTO_PLACEHOLDER)
        self.video_cards.setToolTip("Type the drivers yourself, separated by spaces. Only names from the list above are accepted.")
        self.video_cards.textChanged.connect(lambda _="": self._video_text_changed())
        form.addRow("Or type them", self.video_cards)
        section.add_widget(box)
        self.packages_box = box

    def _build_licences(self, section: AdvancedSection) -> None:
        box, form = _group("Licences")
        self.license_checks: dict[str, QCheckBox] = {}
        flow_host = QWidget()
        flow = FlowLayout(flow_host)
        for group, name, description in licenses.GROUPS:
            check = QCheckBox(f"{name} ({group})")
            check.setToolTip(description)
            check.toggled.connect(lambda _=False: self._license_group_toggled())
            self.license_checks[group] = check
            flow.addWidget(check)
        form.addRow("Accepted groups", flow_host)
        self.licenses = QLineEdit()
        self.licenses.setToolTip("ACCEPT_LICENSE: the groups above written as Portage reads them.")
        self.licenses.textChanged.connect(lambda _="": self._license_text_changed())
        form.addRow("ACCEPT_LICENSE", self.licenses)
        self.package_license = QPlainTextEdit()
        self.package_license.setPlaceholderText("category/package licence, one entry per line")
        self.package_license.setMaximumHeight(70)
        self.package_license.setTabChangesFocus(True)
        self.package_license.setToolTip("Accept a licence for one package only (package.license).")
        form.addRow("Licence for one package", self.package_license)
        section.add_widget(box)
        self.licences_box = box

    def _build_advanced(self, section: AdvancedSection) -> None:
        box, form = _group("Advanced")
        self.emerge_opts = QLineEdit()
        form.addRow("EMERGE_DEFAULT_OPTS", self.emerge_opts)
        self.features = QLineEdit()
        self.features.setPlaceholderText("ccache getbinpkg (space separated)")
        form.addRow("FEATURES", self.features)
        self.world_update = QComboBox()
        self.world_update.addItem("Automatically, when the profile or USE flags differ (recommended)", "auto")
        self.world_update.addItem("Now, during the installation", "now")
        self.world_update.addItem("Later, after the first start", "later")
        self.world_update.setAccessibleName("Update the whole system")
        form.addRow("Update the whole system", _with_help(self.world_update, world.WORLD_HELP, HANDBOOK_WORLD))
        self.depclean = QCheckBox("Remove no longer needed packages (depclean)")
        self.depclean.setToolTip(world.DEPCLEAN_HELP)
        form.addRow(self.depclean)
        section.add_widget(box)
        self.advanced_box = box

    # --- small behaviours ------------------------------------------------------------------------------------------

    def _add_mirror_item(self, url: str, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, url)
        item.setToolTip(url)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.mirror_list.addItem(item)
        return item

    def _move_mirror(self, step: int) -> None:
        row = self.mirror_list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.mirror_list.count():
            return
        item = self.mirror_list.takeItem(row)
        self.mirror_list.insertItem(target, item)
        self.mirror_list.setCurrentRow(target)

    def checked_mirrors(self) -> list[str]:
        """The ticked mirrors in the order shown."""
        return [str(self.mirror_list.item(row).data(Qt.ItemDataRole.UserRole)) for row in range(self.mirror_list.count())
                if self.mirror_list.item(row).checkState() == Qt.CheckState.Checked]

    def _set_checked_mirrors(self, urls: list[str]) -> None:
        known = {str(self.mirror_list.item(row).data(Qt.ItemDataRole.UserRole)): row for row in range(self.mirror_list.count())}
        for url in urls:
            if url not in known:  # a mirror typed into an old settings file: keep it
                self._add_mirror_item(url, url)
        known = {str(self.mirror_list.item(row).data(Qt.ItemDataRole.UserRole)): row for row in range(self.mirror_list.count())}
        # chosen mirrors first, in their order, then the rest as they were
        front = [self.mirror_list.takeItem(known[url]) for url in sorted(set(urls), key=lambda u: -known[u])]
        order = {url: index for index, url in enumerate(urls)}
        front.sort(key=lambda item: order[str(item.data(Qt.ItemDataRole.UserRole))])
        for index, item in enumerate(front):
            self.mirror_list.insertItem(index, item)
        for row in range(self.mirror_list.count()):
            item = self.mirror_list.item(row)
            item.setCheckState(Qt.CheckState.Checked if str(item.data(Qt.ItemDataRole.UserRole)) in order else Qt.CheckState.Unchecked)

    def _source_changed(self) -> None:
        kind = self.source_kind.currentData()
        self.mirror_box.setVisible(kind == mirrors.SOURCE_COUNTRY)
        self.mirror.setVisible(kind == mirrors.SOURCE_CUSTOM)
        if self.mirror_custom_row is not None:
            self.mirror_custom_row.setVisible(kind == mirrors.SOURCE_CUSTOM)
        self.source_note.setVisible(kind == mirrors.SOURCE_AUTOMATIC)

    def _optimize_changed(self) -> None:
        self._update_optimize_note()

    def _portable(self) -> bool:
        try:
            return self.hardware.working_manifest().hardware.target == "portable"
        except Exception:  # noqa: BLE001 - the note is advice; a hardware tab that cannot answer must not break the tab
            return self.manifest.hardware.target == "portable"

    def _update_optimize_note(self) -> None:
        portable = self._portable()
        model = self.optimize.model()
        native_item = model.item(1) if hasattr(model, "item") else None
        if native_item is not None:
            native_item.setEnabled(not portable)
        if portable and self.optimize.currentData() == "native":
            note = optimize.PORTABLE_NATIVE_MESSAGE
        elif portable:
            note = "A portable installation always uses the generic optimisation, so it starts on any PC."
        elif self.optimize.currentData() == "native":
            note = "Programs are built for this exact processor. They may not start on an older PC, and building takes longer than downloading ready-made packages."
        else:
            note = ""
        self.optimize_note.setText(note)
        self.optimize_note.setVisible(bool(note))

    def _flags_changed(self) -> None:
        warnings = optimize.flag_warnings(self.cflags.text())
        self.flags_warning.setText("\n".join(warnings))
        self.flags_warning.setVisible(bool(warnings))

    def _jobs_auto_toggled(self, checked: bool) -> None:
        self.jobs.setEnabled(not checked)
        self.load_limit.setEnabled(not checked)
        self._update_jobs_summary()

    def _swap_mib(self) -> int:
        return self.manifest.storage.swap_mib

    def _update_jobs_summary(self) -> None:
        cpus, memory = os.cpu_count() or 2, system_info._memory_gib()
        shown = f"-j{self.jobs.value()}" if not self.jobs_auto.isChecked() else auto_make_opts(self._swap_mib()).split()[0]
        jobs = int(shown[2:])
        self.jobs_summary.setText(f"Using {jobs} build jobs (your PC has {cpus} threads and {memory:.0f} GiB RAM)")
        self.jobs_auto.setText(f"Automatic ({auto_jobs(self._swap_mib())} jobs for {cpus} processors)")

    def _license_radio(self, which: str) -> None:
        if self._loading_portage:
            return
        self.licenses.setText(licenses.FREE_ONLY if which == "free" else licenses.RECOMMENDED)

    def _license_group_toggled(self) -> None:
        if self._loading_portage:
            return
        chosen = [group for group, _name, _description in licenses.GROUPS if self.license_checks[group].isChecked()]
        self.licenses.setText(licenses.groups_value(chosen))

    def _license_text_changed(self) -> None:
        """The ACCEPT_LICENSE line is the source of truth: the radios and the group boxes follow it."""
        groups = set(licenses.groups_of(self.licenses.text()))
        for group, check in self.license_checks.items():
            check.blockSignals(True)
            check.setChecked(group in groups)
            check.blockSignals(False)
        tokens = self.licenses.text().split()
        choice = "free" if tokens in (["@FREE"], ["-*", "@FREE"]) else \
            "redistributable" if sorted(tokens) == sorted(licenses.RECOMMENDED.split()) else "custom"
        self.license_group.setExclusive(False)
        for button, mine in ((self.license_free, "free"), (self.license_redistributable, "redistributable")):
            button.blockSignals(True)
            button.setChecked(choice == mine)
            button.blockSignals(False)
        self.license_group.setExclusive(True)
        self.license_custom_note.setVisible(choice == "custom")

    def _sync_changed(self) -> None:
        self.rsync_mirror.setEnabled(self.sync_rsync.isChecked())

    # --- USE flags -------------------------------------------------------------------------------------------------

    def _use_tokens(self) -> list[str]:
        return self.use_flags.toPlainText().split()

    def _use_text_changed(self) -> None:
        if not getattr(self, "_loading_portage", True):
            self._fill_use_table()

    def _fill_use_table(self) -> None:
        states = usedesc.states_from_tokens(self._use_tokens())
        query = self.use_search.text().strip()
        if query:
            rows = usedesc.search(query)[:100]
        else:
            table = usedesc.global_flags()
            names = list(dict.fromkeys([*(name for name in sorted(states) if name in table), *(flag for flag in POPULAR_FLAGS if flag in table)]))
            rows = [(name, table[name]) for name in names]
        self.use_table.setRowCount(len(rows))
        for row, (name, description) in enumerate(rows):
            self.use_table.setItem(row, 0, self._cell(name))
            self.use_table.setItem(row, 1, self._cell(description))
            combo = QComboBox()
            for label, value in USE_STATE_LABELS:
                combo.addItem(label, value)
            combo.setCurrentIndex(max(0, combo.findData(states.get(name, "default"))))
            combo.setAccessibleName(f"{name}: on, off or default")
            combo.setMinimumWidth(80)
            combo.currentIndexChanged.connect(lambda _=0, flag=name, box=combo: self._use_state_picked(flag, str(box.currentData())))
            self.use_table.setCellWidget(row, 2, combo)
        self.use_table.resizeColumnToContents(0)
        self.use_table.resizeColumnToContents(2)

    @staticmethod
    def _cell(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        return item

    def _use_state_picked(self, flag: str, state: str) -> None:
        tokens = usedesc.merge_tokens(self._use_tokens(), {flag: state})
        self.use_flags.blockSignals(True)
        self.use_flags.setPlainText("\n".join(tokens))
        self.use_flags.blockSignals(False)

    def _add_custom_flag(self) -> None:
        text = self.use_custom.text().strip()
        problem = usedesc.use_problem(text) if text else ""
        self.use_error.setText(problem)
        self.use_error.setVisible(bool(problem))
        if not text or problem:
            return
        state = "off" if text.startswith("-") else "on"
        self._use_state_picked(text.lstrip("-"), state)
        self.use_custom.clear()
        self._fill_use_table()

    def use_problem(self) -> str:
        """The first USE entry the Handbook (or the syntax) does not allow, in plain words ("" when all is well)."""
        for token in self._use_tokens():
            problem = usedesc.use_problem(token)
            if problem:
                return problem
        return ""

    # --- VIDEO_CARDS -----------------------------------------------------------------------------------------------

    def detected_video_cards(self) -> list[str]:
        """The VIDEO_CARDS values the hardware plan chooses for this computer (known names only)."""
        info = self.hardware.info
        if info is None or not info.hardware:
            return []
        plan = hardware_plan(self.hardware.working_manifest(), info.hardware)
        value = str(dict(plan.make_conf).get("VIDEO_CARDS", "")) if plan is not None else ""
        known = usedesc.video_cards()
        return [word for word in value.split() if word in known]

    def checked_video_cards(self) -> list[str]:
        return [name for name, check in self.video_boxes.items() if check.isChecked()]

    def _set_video_boxes(self, names: list[str]) -> None:
        for name, check in self.video_boxes.items():
            check.blockSignals(True)
            check.setChecked(name in names)
            check.blockSignals(False)

    def _video_box_toggled(self) -> None:
        if self._loading_portage:
            return
        self.video_cards.blockSignals(True)
        self.video_cards.setText(" ".join(self.checked_video_cards()))
        self.video_cards.blockSignals(False)
        self._update_video_note()

    def _video_text_changed(self) -> None:
        if self._loading_portage:
            return
        self._sync_video_boxes()

    def _sync_video_boxes(self) -> None:
        """The boxes show the typed drivers, or what was detected while the field is empty."""
        typed = [word for word in self.video_cards.text().split() if word in self.video_boxes]
        self._set_video_boxes(typed if self.video_cards.text().strip() else self.detected_video_cards())
        self._update_video_note()

    def _update_video_note(self) -> None:
        detected = self.detected_video_cards()
        if self.video_cards.text().strip():
            self.video_note.setText(f"Detected for this computer: {' '.join(detected)}." if detected else "")
        elif detected:
            self.video_note.setText(f"Chosen for this computer: {' '.join(detected)}. Tick other drivers to change them.")
        else:
            self.video_note.setText("The profile's default drivers are used.")

    def video_cards_problem(self) -> str:
        return usedesc.video_cards_problem(self.video_cards.text())

    # --- what an empty field will become ---------------------------------------------------------------------------

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
        if self.optimize.currentData() == "native" and info is not None and info.hardware and not make_conf.get("CPU_FLAGS_X86"):
            make_conf["CPU_FLAGS_X86"] = " ".join(optimize.cpu_flags_of(info.hardware))
        for edit, key in ((self.cpu_flags, "CPU_FLAGS_X86"), (self.video_cards, "VIDEO_CARDS")):
            value = make_conf.get(key, "").replace("${VIDEO_CARDS}", "profile default +")
            if info is None or not info.hardware:
                edit.setPlaceholderText(AUTO_PLACEHOLDER)
            elif value:
                shown = value if len(value) <= 48 else value[:45] + "..."
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (now: {shown})")
            else:
                edit.setPlaceholderText(f"{AUTO_PLACEHOLDER} (the profile default)")
        cpu_level = binhost_rules.cpu_level_of(info.hardware if info is not None else None)
        if cpu_level:
            variant, _note = binhost_rules.choose_variant("auto", cpu_level, self._portable())
            self.binhost_variant.setItemText(0, f"Automatic (now: {'x86-64-v3' if variant == 'v3' else 'generic'} for this processor)")
        self._update_optimize_note()
        self._update_jobs_summary()
        self._sync_video_boxes()

    # --- the manifest ----------------------------------------------------------------------------------------------

    def load_portage(self) -> None:
        self._loading_portage = True
        portage = self.manifest.portage
        self.optimize.setCurrentIndex(max(0, self.optimize.findData(portage.optimize)))
        self.binhost.setChecked(portage.binhost)
        self.binhost_url.setText(portage.binhost_url)
        self.binhost_variant.setCurrentIndex(max(0, self.binhost_variant.findData(portage.binhost_variant)))
        # download sources
        self._source_loaded = self._source_of(portage)
        self.source_kind.setCurrentIndex(max(0, self.source_kind.findData(self._source_loaded)))
        self.mirror.setText("" if self._source_loaded == mirrors.SOURCE_AUTOMATIC else portage.mirror)
        self._set_checked_mirrors(list(portage.mirrors))
        self._source_changed()
        self.sync_rsync.setChecked(self.sync_mode_of(portage))
        self.sync_snapshot.setChecked(not self.sync_mode_of(portage))
        self.rsync_mirror.setText(portage.rsync_mirror)
        self._sync_changed()
        # build jobs
        self._loaded_make_opts = portage.make_opts.strip()
        explicit = self._loaded_make_opts and self._loaded_make_opts != auto_make_opts(self._swap_mib())
        jobs, load = makeopts.parse_make_opts(self._loaded_make_opts) if explicit else (None, None)
        self.jobs_auto.setChecked(not explicit or jobs is None)
        self.jobs.setValue(jobs if jobs is not None else auto_jobs(self._swap_mib()))
        self.load_limit.setValue(int(load) if load else (0 if explicit else (os.cpu_count() or 2)))
        self._loaded_jobs = (self.jobs.value(), self.load_limit.value())
        self._jobs_auto_toggled(self.jobs_auto.isChecked())
        self.cflags.setText(portage.cflags)
        self.cxxflags.setText(portage.cxxflags)
        self.rustflags.setText(portage.rustflags)
        self.emerge_opts.setText(portage.emerge_opts)
        self.features.setText(" ".join(portage.features))
        # licences
        self._loaded_license = licenses.effective_accept_license(portage)
        self.licenses.setText(self._loaded_license)
        self._license_text_changed()
        self.package_license.setPlainText("\n".join(f"{atom} {' '.join(names)}" for atom, names in portage.package_license.items()))
        self.use_flags.setPlainText("\n".join(portage.use_flags))
        self.package_use.setPlainText("\n".join(f"{atom} {' '.join(flags)}" for atom, flags in portage.package_use.items()))
        self.cpu_flags.setText(portage.cpu_flags)
        self.video_cards.setText(portage.video_cards)
        self.world_update.setCurrentIndex(max(0, self.world_update.findData(self.manifest.system.world_update)))
        self.depclean.setChecked(portage.depclean)
        self._loading_portage = False
        self._fill_use_table()
        self._flags_changed()
        self._update_optimize_note()
        self._update_jobs_summary()
        self._sync_video_boxes()

    @staticmethod
    def sync_mode_of(portage: Any) -> bool:
        """True when the settings sync the package tree with rsync (a mirror in an older file counts as asking for it)."""
        return portage.rsync_mode == "sync" or bool(portage.rsync_mirror)

    @staticmethod
    def _source_of(portage: Any) -> str:
        if portage.mirrors:
            return mirrors.SOURCE_COUNTRY
        return mirrors.SOURCE_AUTOMATIC if portage.mirror.rstrip("/") == mirrors.AUTOMATIC else mirrors.SOURCE_CUSTOM

    def source_problem(self) -> str:
        """Why the chosen download source cannot be used ("" when it can)."""
        kind = self.source_kind.currentData()
        if kind == mirrors.SOURCE_CUSTOM:
            return mirrors.url_problem(self.mirror.text()) if self.mirror.text().strip() else "Type the address of the mirror, or choose Automatic."
        if kind == mirrors.SOURCE_COUNTRY and not self.checked_mirrors():
            return "Tick at least one mirror, or choose Automatic."
        return ""

    def rsync_problem(self) -> str:
        text = self.rsync_mirror.text().strip()
        if self.sync_rsync.isChecked() and text and not text.startswith("rsync://"):
            return "The rsync server must be an rsync:// address. HTTPS addresses are not used for the package tree."
        return ""

    def package_license_problems(self) -> list[str]:
        return licenses.license_problems(self.licenses.text(), self._read_package_license())

    def _read_package_license(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for line in self.package_license.toPlainText().splitlines():
            words = line.split()
            if words:
                result[words[0]] = words[1:]
        return result

    def save_portage(self) -> None:
        portage = self.manifest.portage
        portage.optimize = str(self.optimize.currentData())
        portage.binhost = self.binhost.isChecked()
        portage.binhost_url = self.binhost_url.text().strip()
        portage.binhost_variant = str(self.binhost_variant.currentData())
        kind = self.source_kind.currentData()
        if kind == mirrors.SOURCE_AUTOMATIC:
            if self._source_of(portage) != mirrors.SOURCE_AUTOMATIC:
                portage.mirror, portage.mirrors = mirrors.AUTOMATIC, []
        elif kind == mirrors.SOURCE_COUNTRY:
            chosen = self.checked_mirrors()
            if chosen:
                portage.mirrors, portage.mirror = chosen, chosen[0]
        else:
            portage.mirror, portage.mirrors = self.mirror.text().strip().rstrip("/"), []
        portage.rsync_mode = "sync" if self.sync_rsync.isChecked() else "snapshot"
        portage.rsync_mirror = self.rsync_mirror.text().strip() if self.sync_rsync.isChecked() else ""
        if self.jobs_auto.isChecked():
            portage.make_opts = ""  # the installer sizes the jobs from this computer's processors, memory and swap
        elif self._loaded_make_opts and (self.jobs.value(), self.load_limit.value()) == self._loaded_jobs:
            portage.make_opts = self._loaded_make_opts  # untouched: keep the extra flags
        else:
            portage.make_opts = makeopts.format_make_opts(self.jobs.value(), self.load_limit.value() or None)
        portage.cflags = self.cflags.text().strip()
        portage.cxxflags = self.cxxflags.text().strip()
        portage.rustflags = self.rustflags.text().strip()
        portage.emerge_opts = self.emerge_opts.text().strip()
        portage.features = self.features.text().split()
        accept = self.licenses.text().strip()
        if accept != self._loaded_license:  # untouched: the settings file's own groups and value stay
            portage.accept_license = accept
            portage.license_groups = licenses.groups_of(accept)
        portage.package_license = self._read_package_license()
        portage.use_flags = self.use_flags.toPlainText().split()
        portage.package_use = {}
        for line in self.package_use.toPlainText().splitlines():
            words = line.split()
            if words:
                portage.package_use[words[0]] = words[1:]
        portage.cpu_flags = self.cpu_flags.text().strip()
        portage.video_cards = self.video_cards.text().strip()
        self.manifest.system.world_update = str(self.world_update.currentData())
        portage.depclean = self.depclean.isChecked()

    def portage_problem(self) -> str:
        """The first problem of the Portage tab in plain words ("" when there is none). The Handbook's reason comes first for -*."""
        for problem in (self.use_problem(), self.source_problem(), self.rsync_problem(), self.video_cards_problem(), *self.package_license_problems()):
            if problem:
                return problem
        return ""
