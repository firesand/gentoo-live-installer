"""Writes the installed system: a facade over the per-domain configure_*.py modules.

Every name the old single module exported is re-exported here, so ``from gentoo_installerd.configure import X`` keeps working;
the code lives in the module of the team that owns it (see docs/contracts.md, "Module ownership").
"""

from __future__ import annotations

from .configure_base import (  # noqa: F401
    ConfigurationError,
    ConfigurerBase,
    machine_resources,
    RECEIPT_PATH,
    Resources,
    shell_value,
    _plan_list,
    _plan_scalar,
)
from .configure_desktop import (  # noqa: F401
    DesktopAtoms,
    DesktopMixin,
    DesktopResult,
    PREFERRED_FIRST,
)
from .configure_fstab import (  # noqa: F401
    detect_existing_stack,
    ExistingStack,
    FstabMixin,
    LVM_NAME_RE,
    MD_UUID_RE,
    MDADM_ARRAY_RE,
    render_fstab,
    root_stack_problems,
    UNSUPPORTED_ROOT_TYPES,
    _is_md,
    _lvm_volume,
    _md_scan,
    _md_uuid,
)
from .configure_network import (  # noqa: F401
    CERTIFICATE_ROOTS,
    ImportedProfile,
    NetworkMixin,
    render_dhcpcd_static,
    render_netifrc,
    render_nm_ip_sections,
    render_nm_wifi,
    render_nm_wired,
    render_systemd_network,
    rewrite_keyfile,
    _IMPORTABLE_NM_TYPES,
    _keyfile_escape,
    _NM_CERTIFICATE_KEYS,
)
from .configure_portage import (  # noqa: F401
    auto_emerge_jobs,
    auto_make_opts,
    COMPANION_ATOMS,
    DESKTOP_USE,
    MEMORY_ENV_FILE,
    MEMORY_ENV_NAME,
    ninja_opts_from_make_opts,
    PACKAGE_ENV_FILE,
    PACKAGE_USE_ATOM,
    PortageMixin,
    render_make_conf,
    render_package_use,
    _default_use,
    _forbid_use,
    _INCREMENTAL_KEYS,
    _PLAN_FORBIDDEN_KEYS,
    _require_use,
)
from .configure_packages import (  # noqa: F401
    BUILD_LOG_TAIL_BYTES,
    CYCLE_BREAK_RECORD,
    cycle_break_suggestion,
    CYCLE_BREAK_USE,
    EmergeDiagnosis,
    explain_emerge_failure,
    FailedBuild,
    KeepGoingResult,
    MAX_CYCLE_BREAKS,
    NOT_ENOUGH_MEMORY,
    package_of,
    PackageInstallError,
    PackagesMixin,
    _ANSI,
    _BUILD_LOG,
    _build_log_findings,
    _CYCLE_BREAK_RE,
    _DROPPED,
    _FAILED_ENTRY,
    _FAILED_PHASE,
    _FAILED_TO_EMERGE,
    _failure_sentence,
    _LOG_ERROR_PATTERNS,
    _LOG_LINE_CHARS,
    _LOG_LINES,
    _LOG_MEMORY_PATTERN,
    _MEMORY_PATTERN,
    _NUMBERED_ENTRY,
    _PHASES,
    _RESOLUTION_MARKERS,
    _SHORT_OF_RAM,
    _SHORT_OF_SOMETHING,
    _TRANSIENT_PATTERN,
)
from .configure_boot import (  # noqa: F401
    BootMixin,
    check_secure_boot_material,
    efi_arch,
    ESP_SYNC_HOOKS,
    GRUB_EFI_FILES,
    grub_efi_target,
    NVRAM_FAILURE,
    render_esp_sync_hook,
    _ESP_SYNC_TEMPLATE,
)
from .configure_hardware import (  # noqa: F401
    HardwareMixin,
)
from .configure_system import (  # noqa: F401
    account_problems,
    choice_problems,
    generated_locales,
    live_input_problems,
    locale_gen_line,
    option_problems,
    normalised_locale,
    render_dconf_keyboard,
    render_hosts,
    render_kxkbrc,
    render_sshd_config,
    render_xorg_keyboard,
    SystemMixin,
    _keyboard_is_default,
)
from .configure_accounts import (  # noqa: F401
    AccountsMixin,
)
from .configure_gaming import (  # noqa: F401
    GamingMixin,
)


class Configurer(FstabMixin, NetworkMixin, DesktopMixin, GamingMixin, PackagesMixin, HardwareMixin, PortageMixin, BootMixin, SystemMixin, AccountsMixin):
    """The sum of the per-domain mixins; ConfigurerBase holds the shared plumbing."""
