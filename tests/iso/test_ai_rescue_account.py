"""Wave 6: the rescue-ai account, Yama and the polkit rule (iso owner): the fsscript creates the account, verify-iso refuses a stronger one."""
from __future__ import annotations

import json
import re

import pytest

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
FSSCRIPT = (ROOT / "iso/fsscript-stage2.sh").read_text()
FILES = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files"
FORBIDDEN_GROUPS = ["tty", "wheel", "disk", "video", "input", "audio", "plugdev", "usb", "kvm", "portage"]


def test_the_fsscript_makes_rescue_ai_a_locked_system_account_in_no_group_but_its_own() -> None:
    line = next(l for l in FSSCRIPT.splitlines() if l.startswith("useradd ") and "rescue-ai" in l)
    words = line.split()
    assert "-r" in words and "-U" in words and "-m" in words
    assert "-G" not in words and "--groups" not in words and "-g" not in words, "no supplementary group, and its own group is made by -U"
    assert "passwd -l rescue-ai" in FSSCRIPT, "no password, and the account is locked"
    assert "chmod 0700 /home/rescue-ai" in FSSCRIPT
    assert "-k /etc/skel-rescue-ai" in FSSCRIPT, "the skeleton of the gentoo_rescue_ai package"
    # the build stops when anything later gives it a group or a sudo rule
    assert "id -nG rescue-ai" in FSSCRIPT and "rescue-ai must have no sudo or doas rule" in FSSCRIPT


def test_nothing_in_the_iso_sources_gives_rescue_ai_a_group_or_a_sudo_rule() -> None:
    for path in list((ROOT / "iso").rglob("*")) + list(FILES.rglob("*")) + list((ROOT / "packaging").rglob("*.ebuild")):
        if not path.is_file() or path.name in ("fsscript-stage2.sh", "image-contract.json") or path.suffix in (".png", ".svg", ".ico"):
            continue
        for line in path.read_text(errors="replace").splitlines():
            if "rescue-ai" in line and re.search(r"usermod|gpasswd|adduser|sudoers|NOPASSWD|doas|\bwheel\b", line):
                assert line.lstrip().startswith(("#", "//")), f"{path.relative_to(ROOT)}: {line.strip()}"


def account_report(*, passwd: str, group: str, shadow: str = "rescue-ai:!:20000::::::\n", sudoers: str = "root ALL=(ALL:ALL) ALL\n",
                   sudoers_d: dict[str, str] | None = None, doas: str | None = None):
    files = {"/etc/passwd": passwd, "/etc/group": group, "/etc/shadow": shadow, "/etc/sudoers": sudoers}
    entries = {"etc/passwd": "", "etc/group": "", "etc/shadow": "", "etc/sudoers": ""}
    for name, body in (sudoers_d or {}).items():
        files[f"/etc/sudoers.d/{name}"] = body
        entries[f"etc/sudoers.d/{name}"] = ""
    if doas is not None:
        files["/etc/doas.conf"] = doas
        entries["etc/doas.conf"] = ""
    parsed = verify.parse_description("".join(f"file {path} 0644 0 0\n" for path in entries))
    report = verify.Report()
    verify.check_accounts(report, CONTRACT["squashfs"]["accounts"], parsed, lambda path: files[path].encode())
    return report


GOOD_PASSWD = "root:x:0:0:root:/root:/bin/bash\nrescue-ai:x:999:999::/home/rescue-ai:/bin/bash\n"
GOOD_GROUP = "root:x:0:root\nwheel:x:10:gentoo\nrescue-ai:x:999:\n"


def problems(report) -> str:
    return " | ".join(c.detail for c in report.errors)


def test_the_contract_forbids_every_privileged_group_of_the_spec() -> None:
    (rule,) = CONTRACT["squashfs"]["accounts"]
    assert rule["name"] == "rescue-ai" and rule["max_uid"] < 1000 and rule["no_sudo"] is True
    assert set(FORBIDDEN_GROUPS) <= set(rule["forbidden_groups"])


def test_a_weak_rescue_ai_passes() -> None:
    assert account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP).ok


@pytest.mark.parametrize("name", FORBIDDEN_GROUPS)
def test_rescue_ai_in_a_privileged_group_is_refused(name: str) -> None:
    report = account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP + f"{name}:x:50:gentoo,rescue-ai\n")
    assert not report.ok and name in problems(report) and "must never be" in problems(report)


def test_rescue_ai_in_any_other_group_is_refused_too() -> None:
    assert "it is in the groups games" in problems(account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP + "games:x:35:rescue-ai\n"))


def test_rescue_ai_with_a_user_id_of_a_person_or_the_wrong_primary_group_is_refused() -> None:
    assert "not a system uid" in problems(account_report(passwd=GOOD_PASSWD.replace(":999:999:", ":1001:999:"), group=GOOD_GROUP))
    assert "primary group" in problems(account_report(passwd=GOOD_PASSWD.replace(":999:999:", ":999:100:"), group=GOOD_GROUP))


def test_an_unlocked_or_missing_password_entry_is_refused() -> None:
    assert "not locked" in problems(account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, shadow="rescue-ai::20000::::::\n"))
    assert "not locked" in problems(account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, shadow="root:!:1::::::\n"))
    assert account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, shadow="rescue-ai:*:20000::::::\n").ok


def test_a_missing_account_is_refused() -> None:
    assert "not in /etc/passwd" in problems(account_report(passwd="root:x:0:0:::\n", group=GOOD_GROUP))


@pytest.mark.parametrize(
    "rule",
    [
        "rescue-ai ALL=(ALL) NOPASSWD: ALL", "%rescue-ai ALL=(ALL) ALL", "root,rescue-ai ALL=(ALL) ALL", "Defaults:rescue-ai !authenticate",
        "User_Alias AGENTS = rescue-ai", "#999 ALL=(ALL) ALL", "ALL ALL=(ALL) NOPASSWD: ALL", "ALL,!root ALL=(ALL) ALL",
    ],
)
def test_a_sudo_rule_that_covers_rescue_ai_is_refused_in_sudoers_and_in_the_drop_ins(rule: str) -> None:
    assert not account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, sudoers=f"root ALL=(ALL:ALL) ALL\n{rule}\n").ok
    assert not account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, sudoers_d={"90-x": f"{rule}\n"}).ok


def test_the_live_users_wheel_rule_and_comments_are_fine() -> None:
    sudoers = "# rescue-ai is not here\n#includedir /etc/sudoers.d\n@includedir /etc/sudoers.d\nroot ALL=(ALL:ALL) ALL\n%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n"
    assert account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, sudoers=sudoers, doas="permit persist :wheel\n").ok


def test_a_doas_rule_for_everybody_is_refused() -> None:
    assert not account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, doas="permit nopass keepenv root\npermit nopass setenv { PATH } rescue-ai\n").ok
    assert not account_report(passwd=GOOD_PASSWD, group=GOOD_GROUP, doas="permit nopass keepenv\n").ok


def test_the_account_check_runs_for_every_image() -> None:
    """check_squashfs calls it: an image without the account fails 'accounts.exists' even with the rest in place."""
    report = verify.Report()
    verify.check_squashfs(report, verify.parse_description("dir etc 0755 0 0\n"), CONTRACT, [], "release", lambda p: b"", "UID: 0\nGID: 0", (), lambda p: False)
    assert "accounts.exists" in {c.id for c in report.errors}


# --- Yama in the live kernel ---------------------------------------------------------------------

YAMA_FRAGMENT = (FILES / "live-rescue-ai.config").read_text()


def kernel_report(config: str):
    report = verify.Report()
    verify.check_kernel_security(report, config, CONTRACT)
    return report


def test_the_kernel_fragment_satisfies_the_contract() -> None:
    report = kernel_report(YAMA_FRAGMENT)
    assert report.ok and [c.id for c in report.checks] == ["kernel.yama"]


@pytest.mark.parametrize(
    "config",
    [
        "# CONFIG_SECURITY_YAMA is not set\nCONFIG_SECURITY=y\nCONFIG_LSM=\"landlock,yama\"\n",
        "CONFIG_SECURITY=y\nCONFIG_SECURITY_YAMA=y\nCONFIG_LSM=\"landlock,lockdown,integrity,bpf\"\n",  # built, but not enabled
        "CONFIG_SECURITY=y\nCONFIG_SECURITY_YAMA=y\n",
        "CONFIG_SECURITY=y\nCONFIG_SECURITY_YAMA=m\nCONFIG_LSM=\"yama\"\n",
        "",
    ],
)
def test_a_kernel_without_yama_in_its_lsm_list_is_refused(config: str) -> None:
    report = kernel_report(config)
    assert not report.ok and "ptrace_scope" in problems(report)


def test_yama_only_in_the_middle_of_the_list_counts() -> None:
    assert kernel_report('CONFIG_SECURITY=y\nCONFIG_SECURITY_YAMA=y\nCONFIG_LSM="selinux,yama,bpf"\n').ok
    assert not kernel_report('CONFIG_SECURITY=y\nCONFIG_SECURITY_YAMA=y\nCONFIG_LSM="notyama,yamax"\n').ok


def test_the_full_run_reads_the_kernel_configuration_for_yama() -> None:
    """verify() hands boot/gentoo-config to the Yama check as well as to the display check."""
    source = (ROOT / "scripts/verify-iso.py").read_text()
    assert re.search(r"check_kernel_config\(report, config_text, contract, initramfs\)\s+check_kernel_security\(report, config_text, contract\)", source)


def test_sysctl_overlay_and_the_boot_runlevel_apply_yama() -> None:
    conf = (ROOT / "iso/root_overlay/etc/sysctl.d/60-gentoo-rescue-ai.conf").read_text()
    assert re.search(r"(?m)^kernel\.yama\.ptrace_scope\s*=\s*1$", conf)
    assert ["sysctl", "boot"] in CONTRACT["squashfs"]["implicit_services"]
    rules = [r for r in CONTRACT["squashfs"]["content"] if r["path"] == "etc/sysctl.d/60-gentoo-rescue-ai.conf"]
    assert rules and all(re.search(p, conf, re.M) for p in rules[0]["must_match"])


def test_the_polkit_rule_refuses_the_agents_before_any_other_rule() -> None:
    rules = ROOT / "iso/root_overlay/etc/polkit-1/rules.d"
    assert sorted(p.name for p in rules.iterdir())[0] == "00-gentoo-rescue-ai.rules", "the first answer wins, so this file is read first"
    text = (rules / "00-gentoo-rescue-ai.rules").read_text()
    assert 'subject.user == "rescue-ai"' in text and "polkit.Result.NO" in text


def _su_rule() -> dict:
    return next(r for r in CONTRACT["squashfs"]["content"] if r["path"] == "etc/pam.d/su")


def _su_ok(body: str) -> bool:
    rule = _su_rule()
    return all(re.search(p, body, re.M) for p in rule["must_match"]) and not any(re.search(p, body, re.M) for p in rule["must_not_match"])


GOOD_SU = "auth\t\tsufficient\tpam_rootok.so\nauth\t\trequired\tpam_wheel.so use_uid\nauth\t\tinclude\t\tsystem-auth\n"


def test_su_must_be_closed_to_non_wheel_users_by_pam_wheel() -> None:
    """rescue-ai is not in wheel; su to the live user (empty password, passwordless sudo) must stop at pam_wheel."""
    assert _su_ok("auth\t\trequired\tpam_wheel.so use_uid\nauth\tinclude\tsystem-auth\n")
    assert _su_ok(GOOD_SU)


@pytest.mark.parametrize("body", [
    "auth\tinclude\tsystem-auth\n",
    "# auth required pam_wheel.so use_uid\nauth include system-auth\n",
    "auth required pam_wheel.so use_uid root_only\n",
    "auth sufficient pam_wheel.so trust use_uid\nauth required pam_wheel.so use_uid\n",
    "auth required pam_wheel.so\n",
])
def test_a_su_without_a_working_pam_wheel_is_refused(body: str) -> None:
    assert not _su_ok(body)


def test_the_fsscript_stops_the_build_when_su_is_not_closed() -> None:
    assert "/etc/pam.d/su" in FSSCRIPT and "pam_wheel" in FSSCRIPT
    assert FSSCRIPT.index("pam_wheel\\.so") < FSSCRIPT.index("Everything the installer, the repair tool")


def test_the_readme_dd_command_is_one_pasteable_line_with_sudo_and_makes_no_untested_claim_about_ventoy() -> None:
    text = (ROOT / "iso/README-live.txt").read_text()
    line = next(l for l in text.splitlines() if "dd if=" in l)
    assert "sudo dd if=gentoo-installer.iso of=/dev/sdX" in line and "status=progress" in line and "conv=fsync" in line
    assert not line.rstrip().endswith("\\") and "conv=fsync" in line.split("dd if=")[1]
    assert not re.search(r"Ventoy-style|(?<!not tested with this disc\.)\bVentoy\b.*do not give", text)
    assert "Ventoy" not in text.split("do not give a bootable disc")[0].splitlines()[-1]
