"""iso/fsscript-stage2.sh runs inside the stage2 chroot: its checks must agree with the rest of the build."""
from __future__ import annotations

import json
import re

from .conftest import ROOT

FSSCRIPT = (ROOT / "iso/fsscript-stage2.sh").read_text()
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())


def required_commands() -> list[str]:
    block = re.search(r"required_commands=\(\n(.*?)\n\)", FSSCRIPT, re.S)
    assert block, "required_commands array not found"
    words: list[str] = []
    for line in block.group(1).splitlines():
        line = line.split("#")[0]
        words += line.split()
    return words


def test_fsscript_checks_exactly_the_commands_the_contract_requires() -> None:
    """A program that fsscript demands but the contract does not list (or the reverse) is a drifting rule."""
    in_script = set(required_commands())
    in_contract = set(CONTRACT["squashfs"]["commands"])
    # these are about the desktop, not the build: the contract looks for them, the fsscript has no need to
    only_contract = {"loadkeys", "nmtui", "nmcli"}
    assert in_script - in_contract == set(), f"fsscript requires {sorted(in_script - in_contract)} but the contract does not"
    assert in_contract - in_script == only_contract, sorted(in_contract - in_script - only_contract)
    assert len(required_commands()) == len(in_script), "a command is listed twice"


def test_fsscript_fails_the_build_instead_of_continuing() -> None:
    assert "set -eo pipefail" in FSSCRIPT
    assert re.search(r'fail\(\) \{\n\techo "\$\*" >&2\n\texit 1\n\}', FSSCRIPT)
    for needle in ("Required live application is missing", "Required live tool is missing", "Memtest86+ image is missing"):
        assert needle in FSSCRIPT


def test_fsscript_creates_the_target_and_rescue_directories_and_the_live_user() -> None:
    assert re.search(r"^install -d -m 0755 /mnt/gentoo$", FSSCRIPT, re.M)
    assert re.search(r"^install -d -m 0700 /mnt/rescue$", FSSCRIPT, re.M), "the repair sessions are private to root"
    assert "passwd -d gentoo" in FSSCRIPT and "passwd -l root" in FSSCRIPT
    assert "usermod -a -G gentoo-installer gentoo" in FSSCRIPT
    paths = {item["path"]: item for item in CONTRACT["squashfs"]["required"]}
    assert paths["mnt/rescue"]["type"] == "dir" and paths["mnt/gentoo"]["type"] == "dir"
    assert paths["mnt/rescue"]["mode"] == "0700" and "mode" not in paths["mnt/gentoo"], "the contract checks the mode the fsscript sets"


def test_fsscript_checks_files_that_the_overlay_provides() -> None:
    for path in re.findall(r"(/etc/[A-Za-z0-9_./-]+)", FSSCRIPT):
        # created by the fsscript itself, by the base system or by packages
        if path in ("/etc/profile", "/etc/gentoo-installer-vm-test") or path.startswith(("/etc/init.d/", "/etc/conf.d/display-manager")):
            continue
        relative = path.lstrip("/")
        assert (ROOT / "iso/root_overlay" / relative).exists(), f"fsscript refers to {path}, which the overlay does not provide"


def test_fsscript_memtest_files_are_the_ones_the_menu_and_the_iso_need() -> None:
    for name in ("memtest64.bios", "memtest.efi64"):
        assert f"/boot/memtest86plus/{name}" in FSSCRIPT
        assert name in CONTRACT["iso"]["required_files"]
        assert f"boot/memtest86plus/{name}" in {i["path"] for i in CONTRACT["squashfs"]["required"]}


def test_rescue_scripts_are_only_a_warning() -> None:
    """scripts/verify-iso.py fails the build for a console script that pyproject.toml declares; the fsscript only warns."""
    block = FSSCRIPT[FSSCRIPT.index("if compgen -G '/usr/lib/python3*/site-packages/gentoo_installerd/rescue"):]
    scripts = block.split("# The repair tool falls back")[0]
    assert "warning:" in scripts
    assert "fail " not in scripts
    for command in ("/usr/bin/gentoo-rescue", "/usr/bin/gentoo-chroot", "/usr/bin/gentoo-installer-text"):
        assert command in scripts, command
    assert "gentoo_installer/textui/__init__.py" in scripts, "the text installer is checked where the package has it"


def test_the_live_grub_is_checked_for_both_platforms_of_the_fallback_install() -> None:
    """The repair tool runs the live grub-install when the repaired system cannot run its own."""
    assert "/usr/lib/grub/i386-pc" in FSSCRIPT and "/usr/lib/grub/x86_64-efi" in FSSCRIPT
    paths = {item["path"]: item for item in CONTRACT["squashfs"]["required"]}
    assert paths["usr/lib/grub/i386-pc"]["type"] == paths["usr/lib/grub/x86_64-efi"]["type"] == "dir"


def test_the_polkit_agent_of_the_live_session_is_required() -> None:
    assert "/usr/libexec/polkit-gnome-authentication-agent-1" in FSSCRIPT
    assert "usr/libexec/polkit-gnome-authentication-agent-1" in {item["path"] for item in CONTRACT["squashfs"]["required"]}
