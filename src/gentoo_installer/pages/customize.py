"""CORE: the Customize page, a host for the Hardware tab and the Portage, Network and Applications && tools tabs.

Each tab lives in its own module (portage_tab.py, network_tab.py, tools_tab.py) as a mixin of CustomizePage;
the names the module used to define are re-exported here.
"""

from __future__ import annotations

import os  # noqa: F401 - tests patch customize.os
from typing import Any

from PySide6.QtWidgets import QTabWidget

from .. import system_info  # noqa: F401 - tests patch customize.system_info
from ..manifest import InstallManifest
from ..widgets import Page
from .hardware import HardwareTab, hardware_plan
from .network_tab import NetworkTabMixin
from .portage_tab import AUTO_PLACEHOLDER, PortageTabMixin, auto_jobs, auto_make_opts, parse_jobs  # noqa: F401
from .readiness import proxy_problem
from .tools_tab import (  # noqa: F401
    COMPANIONS,
    HWMONUX_ADVICE,
    HWMONUX_LOW_MEMORY,
    HWMONUX_MEMORY_GIB,
    KDE_APPS,
    ToolsTabMixin,
    hwmonux_memory_note,
)


class CustomizePage(PortageTabMixin, NetworkTabMixin, ToolsTabMixin, Page):
    def __init__(self, manifest: InstallManifest, info: system_info.SystemInfo | None = None):
        super().__init__("Make it yours", "Safe defaults are already selected. The Hardware tab shows what was found on this computer; the other tabs use the same Portage and network configuration files described in the Handbook.")
        self.manifest = manifest
        self._loaded_make_opts = ""
        self._cleared_companions: tuple[bool, bool] | None = None
        tabs = QTabWidget()
        self.hardware = HardwareTab(manifest, info)
        tabs.addTab(self.hardware, "Hardware")
        tabs.addTab(self._portage_tab(), "Portage")
        tabs.addTab(self._network_tab(), "Network")
        tabs.addTab(self._extras_tab(), "Applications && tools")
        self.tabs = tabs
        self.content.addWidget(tabs)
        self.hardware.changed.connect(self._hardware_changed)
        self.load_manifest()
        self.finish_layout()

    # --- the hardware tab ----------------------------------------------------------------------------------

    def set_info(self, info: system_info.SystemInfo) -> None:
        self.hardware.set_info(info)
        self.refresh_placeholders()
        self._update_network_notes()
        self._update_hwmonux_note()
        names = sorted({nic.iface for nic in info.profile.nics if nic.iface})
        if names:
            self.interface.set_names(names)

    def set_advanced(self, advanced: bool) -> None:
        self.hardware.set_advanced(advanced)
        self.set_network_advanced(advanced)

    def show_hardware(self) -> None:
        self.tabs.setCurrentWidget(self.hardware)

    def reveal_section(self, section: str) -> None:
        """Bring the tab to the front that holds the settings of a validation section."""
        if section == "hardware":
            self.show_hardware()
        elif section == "portage":
            self.tabs.setCurrentIndex(1)
        elif section == "network":
            self.tabs.setCurrentIndex(2)
        elif section == "options":
            self.tabs.setCurrentIndex(3)

    def _hardware_changed(self) -> None:
        """A hardware choice was edited: what 'automatic' means in the Portage tab may have changed."""
        self.refresh_placeholders()
        self._update_network_notes()

    def load_manifest(self) -> None:
        self.load_portage()
        self.load_network()
        self.load_tools()
        self._network_changed()
        self._network_manager_changed()
        self.hardware.load_manifest()
        self.refresh_placeholders()

    def save_manifest(self) -> None:
        self.save_portage()
        self.save_network()
        self.save_tools()
        self.hardware.save_manifest()

    def validate_page(self) -> tuple[bool, str]:
        self.save_manifest()
        problem = proxy_problem(self.proxy.text())
        if problem:
            return False, problem
        sections = self.manifest.validate_sections()
        errors = [error for name in ("portage", "network", "options") for error in sections[name]]
        if errors:
            return False, errors[0]
        hardware = self.hardware.problems()
        if hardware:
            self.show_hardware()
            return False, hardware[0]
        return True, ""

