"""scripts/verify-iso.py: parsers, rules (one positive and one negative case each) and an end-to-end run."""
from __future__ import annotations

import copy
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from .conftest import FIXTURES, ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
render = load_script("iso/render-config.py", "render_config")
wrapper_keys = load_script("iso/make_conf_keys.py", "make_conf_keys")
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())
LISTING = (FIXTURES / "squashfs_listing_r19_excerpt.txt").read_text()


def failed(report) -> set[str]:
    return {c.id for c in report.errors}


# --- small parsers -------------------------------------------------------------------------


def test_parse_spec_follows_catalyst_rules() -> None:
    values = verify.parse_spec('a: one\nb: x y # comment\n# whole line comment\nc:\n\tp|q\n\tr|s\nd: "quoted"\n')
    assert values == {"a": "one", "b": ["x", "y"], "c": ["p|q", "r|s"], "d": "quoted"}


def test_flavor_volume_id() -> None:
    assert verify.flavor_volume_id("GENTOO_INSTALLER_AMD64", "release") == "GENTOO_INSTALLER_AMD64"
    assert verify.flavor_volume_id("GENTOO_INSTALLER_AMD64", "testkey") == "GENTOO_INSTALLER_TESTKEY"
    assert verify.flavor_volume_id("GENTOO_INSTALLER_TESTKEY", "testkey") == "GENTOO_INSTALLER_TESTKEY"


def test_flavor_names_agree_with_the_shell_library(pins: dict[str, str]) -> None:
    """scripts/build-iso.sh and the builder use naming.sh; the verifier must expect the same label."""
    script = f"source {ROOT}/iso/lib/naming.sh; ISO_NAME=x-1.iso; ISO_VOLID={pins['ISO_VOLID']}; iso_apply_flavor testkey && echo $ISO_NAME $ISO_VOLID"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["x-1-TESTKEY.iso", verify.flavor_volume_id(pins["ISO_VOLID"], "testkey")]


def test_load_pins_reads_quoted_values(pins: dict[str, str]) -> None:
    assert pins["ISO_BOOTARGS"] == "nodhcp secureconsole"
    assert pins["ISO_VOLID"] == "GENTOO_INSTALLER_AMD64"
    assert len(pins["GENTOO_TREEISH_SIGNER_FPR"]) == 40


def test_parse_el_torito_of_the_accepted_image() -> None:
    boot = verify.parse_el_torito((FIXTURES / "el_torito_r19.txt").read_text())
    assert boot.volume_id == "GENTOO_INSTALLER_AMD64"
    assert boot.images == [("BIOS", "/boot/grub/i386-pc/eltorito.img"), ("UEFI", "/efi.img")]
    assert "grub2-mbr" in boot.boot_record and "GPT" in boot.boot_record


def test_parse_description_handles_quoted_names_and_links() -> None:
    entries = verify.parse_description(
        'dir etc 0755 0 0\nfile "usr/bin/with space" 0755 0 0\n'
        'slink etc/runlevels/default/foo 0777 0 0 /etc/init.d/foo\nslink "a b" 0777 0 0 "../x y"\n'
        'file "q\\"uote" 0644 0 0\n'
    )
    assert entries["usr/bin/with space"].kind == "file"
    assert entries["etc/runlevels/default/foo"].target == "/etc/init.d/foo"
    assert entries["a b"].target == "../x y"
    assert 'q"uote' in entries
    with pytest.raises(verify.VerifyError):
        verify.parse_description("garbage line\n")


def test_parse_description_reads_the_real_listing() -> None:
    entries = verify.parse_description(LISTING)
    assert entries["etc"].uid == 1000 and entries["etc/init.d/xdm"].mode == 0o755
    assert entries["etc/runlevels/default/gentoo-installerd"].kind == "slink"
    packages = verify.installed_packages(entries)
    assert "sys-fs/lvm2-2.03.39" in packages
    assert "sys-fs/lvm2" in verify.package_names(packages)
    assert "net-misc/chrony" in verify.package_names(packages)


@pytest.mark.parametrize(
    ("pf", "name"),
    [("lvm2-2.03.39", "lvm2"), ("xorg-server-21.1.9-r1", "xorg-server"), ("7zip-24.09", "7zip"), ("gtk+-3.24.43", "gtk+"),
     ("docbook-xml-dtd-4.5-r2", "docbook-xml-dtd"), ("util-linux-2.42.3", "util-linux"), ("gcc-15.2.1_p20251122", "gcc"),
     ("memtest86+-7.20", "memtest86+")],
)
def test_package_names(pf: str, name: str) -> None:
    assert verify.package_names([f"x/{pf}"]) == {f"x/{name}"}


# --- ISO level -----------------------------------------------------------------------------

ISO_CONTRACT = CONTRACT["iso"]
GOOD_FILES = [f"/{name}" for name in ISO_CONTRACT["required_files"]] + [
    "/boot/grub/i386-pc", "/boot/grub/x86_64-efi/a.mod", "/boot/grub/i386-efi/a.mod", "/boot/grub/themes/gentoo_frosted/theme.txt",
    "/boot/grub/i386-pc/eltorito.img",
]


def iso_report(boot_text: str | None = None, files=GOOD_FILES, volid="GENTOO_INSTALLER_AMD64"):
    report = verify.Report()
    boot = verify.parse_el_torito(boot_text or (FIXTURES / "el_torito_r19.txt").read_text())
    verify.check_iso(report, files, boot, ISO_CONTRACT, volid)
    return report


def test_iso_checks_pass_for_the_accepted_layout() -> None:
    assert iso_report().ok


def test_iso_needs_both_boot_images() -> None:
    text = (FIXTURES / "el_torito_r19.txt").read_text().replace("UEFI", "BIOS")
    assert "iso.el_torito" in failed(iso_report(text))
    text = "\n".join(l for l in (FIXTURES / "el_torito_r19.txt").read_text().splitlines() if "UEFI" not in l and "/efi.img" not in l)
    assert "iso.el_torito" in failed(iso_report(text))


def test_iso_must_be_hybrid_for_usb_sticks() -> None:
    text = (FIXTURES / "el_torito_r19.txt").read_text().replace("MBR protective-msdos-label grub2-mbr cyl-align-off GPT APM", "")
    assert "iso.hybrid" in failed(iso_report(text))


def test_iso_volume_label_must_match() -> None:
    assert "iso.volume_id" in failed(iso_report(volid="GENTOO_INSTALLER_TESTKEY"))


@pytest.mark.parametrize("missing", ["efi/boot/bootia32.efi", "memtest64.bios", "SECURE-BOOT.txt", "boot/gentoo.igz"])
def test_iso_required_files(missing: str) -> None:
    files = [f for f in GOOD_FILES if f != "/" + missing]
    report = iso_report(files=files)
    assert "iso.required_files" in failed(report)
    assert missing in next(c.detail for c in report.errors if c.id == "iso.required_files")


# --- the squashfs --------------------------------------------------------------------------

EXTRA = """\
dir etc/lvm 0755 0 0
file etc/lvm/lvmlocal.conf 0644 0 0
dir etc/runlevels/default 0755 0 0
slink etc/runlevels/default/chronyd 0777 0 0 /etc/init.d/chronyd
file etc/init.d/chronyd 0755 0 0
"""


def squashfs_report(listing: str, *, flavor="testkey", rcadd=("gentoo-installerd|default", "NetworkManager|default"), files=None,
                    builder_ids=(), source=lambda p: False, contract=CONTRACT):
    files = files or {}
    report = verify.Report()
    entries = verify.parse_description(listing)

    def read_file(path: str) -> bytes:
        if path in files:
            return files[path].encode()
        raise verify.VerifyError(f"no content for {path}")

    verify.check_squashfs(report, entries, contract, list(rcadd), flavor, read_file, "UID: 0 (index = 1)\nGID: 0 (index = 1)", builder_ids, source)
    return report


def test_builder_owned_overlay_is_found() -> None:
    """I10: the r19 image has 43 overlay entries owned by uid/gid 1000; the same listing as root passes."""
    report = squashfs_report(LISTING)
    assert "squashfs.ownership" in failed(report)
    fixed = "\n".join(l.replace(" 1000 1000", " 0 0") for l in LISTING.splitlines())
    assert "squashfs.ownership" not in failed(squashfs_report(fixed))


def test_live_user_may_own_its_home_and_mailbox() -> None:
    listing = "dir home/gentoo 0700 1000 100\nfile home/gentoo/.bashrc 0644 1000 100\nfile var/spool/mail/gentoo 0660 1000 12\n"
    assert "squashfs.ownership" not in failed(squashfs_report(listing))
    assert "squashfs.ownership" in failed(squashfs_report(listing + "dir srv 0755 1000 100\n"))


def test_other_builder_uid_is_found_too() -> None:
    assert "squashfs.ownership" in failed(squashfs_report("dir etc 0755 1234 1234\n", builder_ids=(1234,)))
    assert "squashfs.ownership" not in failed(squashfs_report("dir etc 0755 123 123\n", builder_ids=(1234,)))


def test_every_rcadd_service_needs_a_link_and_an_init_script() -> None:
    ok = squashfs_report(LISTING, rcadd=("gentoo-installerd|default", "NetworkManager|default", "dbus|default", "elogind|boot", "udev|sysinit"))
    assert "squashfs.services" not in failed(ok)
    missing_link = squashfs_report(LISTING, rcadd=("gentoo-installerd|default", "chronyd|default"))
    assert "chronyd is not in runlevel default" in next(c.detail for c in missing_link.errors if c.id == "squashfs.services")
    wrong_level = squashfs_report(LISTING, rcadd=("gentoo-installerd|boot",))
    assert "squashfs.services" in failed(wrong_level)
    no_script = squashfs_report(LISTING + "slink etc/runlevels/default/ghost 0777 0 0 /etc/init.d/ghost\n", rcadd=("ghost|default",))
    assert "etc/init.d/ghost does not exist" in next(c.detail for c in no_script.errors if c.id == "squashfs.services")


def test_forbidden_services_and_paths() -> None:
    assert "squashfs.forbidden_services" in failed(squashfs_report(LISTING))  # acpid is enabled in r19
    assert "squashfs.forbidden_services" not in failed(squashfs_report(LISTING.replace("etc/runlevels/default/acpid", "etc/runlevels/default/other")))
    report = squashfs_report(LISTING + "file opt/rust-bin-1.98.1/bin/rustc 0755 0 0\n")
    forbidden = next(c.detail for c in report.errors if c.id == "squashfs.forbidden")
    assert "opt/rust-bin-1.98.1/bin/rustc" in forbidden and "var/cache/binhost" in forbidden and "etc/portage/make.conf.old" in forbidden
    clean = "\n".join(l for l in LISTING.splitlines() if "rust-bin" not in l and "binhost" not in l and "make.conf." not in l)
    assert "squashfs.forbidden" not in failed(squashfs_report(clean))


def test_a_leftover_empty_directory_is_not_a_forbidden_file() -> None:
    """Portage keeps a directory that holds a file it does not own; the check is about files."""
    clean = "\n".join(l for l in LISTING.splitlines() if "rust-bin" not in l and "binhost" not in l and "make.conf." not in l)
    assert "squashfs.forbidden" not in failed(squashfs_report(clean + "\ndir opt/rust-bin-1.98.1 0755 0 0\ndir opt/rust-bin-1.98.1/lib 0755 0 0\n"))
    assert "squashfs.forbidden" in failed(squashfs_report(clean + "\ndir opt/rust-bin-1.98.1 0755 0 0\nfile opt/rust-bin-1.98.1/bin/rustc 0755 0 0\n"))


def test_release_flavour_refuses_test_access_and_needs_a_locked_root() -> None:
    shadow = {"/etc/shadow": "root:!:20000::::::\ngentoo::20000:0:99999:7:::\n"}
    release_with_key = LISTING + "file root/.ssh/authorized_keys 0600 0 0\n"
    assert "flavor.release" in failed(squashfs_report(release_with_key, flavor="release", files=shadow))
    assert "flavor.release" not in failed(squashfs_report(LISTING.replace("etc/runlevels/default/sshd", "x/y"), flavor="release", files=shadow))
    # I9: sshd itself is on every flavour now; only the test key, its drop-in and host keys are test access
    assert "flavor.release" not in failed(squashfs_report(LISTING, flavor="release", files=shadow))
    assert "flavor.release" in failed(squashfs_report(LISTING + "file etc/ssh/ssh_host_ed25519_key 0600 0 0\n", flavor="release", files=shadow))
    unlocked = {"/etc/shadow": "root::20000::::::\n"}
    assert "flavor.root_locked" in failed(squashfs_report(LISTING, flavor="release", files=unlocked))
    assert "flavor.root_locked" not in failed(squashfs_report(LISTING, flavor="release", files=shadow))
    star = {"/etc/shadow": "root:*:20000::::::\n"}
    assert "flavor.root_locked" not in failed(squashfs_report(LISTING, flavor="release", files=star))


def test_testkey_flavour_needs_all_test_access_files() -> None:
    assert "flavor.testkey" in failed(squashfs_report(LISTING, flavor="testkey"))
    complete = LISTING + "file root/.ssh/authorized_keys 0600 0 0\nfile etc/gentoo-installer-vm-test 0644 0 0\nfile etc/ssh/sshd_config.d/90-installer-vm-test.conf 0644 0 0\n"
    assert "flavor.testkey" not in failed(squashfs_report(complete, flavor="testkey"))


def test_content_rules() -> None:
    good = {
        "/etc/lvm/lvmlocal.conf": "global {\n\tevent_activation = 0\n}\nactivation {\n\tauto_activation_volume_list = [ ]\n}\n",
        "/etc/mdadm.conf": "# x\nAUTO -all\n",
        "/etc/portage/make.conf": 'VIDEO_CARDS="amdgpu"\nFEATURES="strict"\n',
        "/etc/chrony/chrony.conf": "pool x iburst\nmakestep 1.0 -1\n",
    }
    base = LISTING + EXTRA + "file etc/mdadm.conf 0644 0 0\ndir etc/chrony 0755 0 0\nfile etc/chrony/chrony.conf 0644 0 0\n"
    ok = squashfs_report(base, files=good)
    assert not [c for c in ok.errors if c.id == "squashfs.content" and ("lvmlocal" in c.detail or "mdadm" in c.detail or "make.conf" in c.detail or "chrony" in c.detail)]
    bad = dict(good, **{"/etc/lvm/lvmlocal.conf": "global {\n}\n", "/etc/portage/make.conf": 'MAKEOPTS="-j4"\nFEATURES="buildpkg"\nVIDEO_CARDS="x"\n',
                        "/etc/chrony/chrony.conf": "makestep 1\nrtcsync\n"})
    details = " | ".join(c.detail for c in squashfs_report(base, files=bad).errors if c.id == "squashfs.content")
    assert "event_activation" in details and "MAKEOPTS" in details and "buildpkg" in details and "rtcsync" in details


def test_rescue_items_apply_only_when_the_source_has_the_tool() -> None:
    names = lambda report: next((c.detail for c in report.errors if c.id == "squashfs.required"), "")  # noqa: E731
    without = squashfs_report(LISTING)
    assert "usr/bin/gentoo-rescue" not in names(without) and "gentoo-rescue.desktop" not in names(without)
    package_only = squashfs_report(LISTING, source=lambda p: p == "src/gentoo_installerd/rescue/__init__.py")
    assert "usr/share/applications/gentoo-rescue.desktop" in names(package_only)
    assert "usr/bin/gentoo-rescue" not in names(package_only).replace("usr/share/applications/gentoo-rescue.desktop", ""), "console scripts follow pyproject.toml"


def test_console_scripts_are_required_only_when_pyproject_declares_them() -> None:
    def report(declared: str):
        entries = verify.parse_description(LISTING)
        result = verify.Report()
        verify.check_squashfs(result, entries, CONTRACT, [], "testkey", lambda p: b"", "UID: 0\nGID: 0", (), lambda p: False,
                              lambda path, text: path == "pyproject.toml" and text in declared)
        return next((c.detail for c in result.errors if c.id == "squashfs.required"), "")

    def missing(declared: str) -> set[str]:
        return {name.strip() for name in report(declared).removeprefix("missing from the image: ").split(",")}

    assert "usr/bin/gentoo-rescue" not in missing("")
    cli_only = missing('gentoo-rescue = "x"')
    assert "usr/bin/gentoo-rescue" in cli_only
    assert "usr/bin/gentoo-rescue-gui" not in cli_only and "usr/bin/gentoo-installer-text" not in cli_only
    both = missing('gentoo-rescue = "x"\ngentoo-rescue-gui = "y"')
    assert "usr/bin/gentoo-rescue-gui" in both
    text_installer = missing('gentoo-installer-text = "gentoo_installer.textui.cli:main"')
    assert "usr/bin/gentoo-installer-text" in text_installer and "usr/bin/gentoo-rescue" not in text_installer


def test_gentoo_chroot_follows_the_rescue_package_not_pyproject() -> None:
    """The ebuild installs the wrapper from FILESDIR whenever the sources contain the repair tool."""
    def missing(source) -> str:
        result = verify.Report()
        verify.check_squashfs(result, verify.parse_description(LISTING), CONTRACT, [], "testkey", lambda p: b"", "UID: 0\nGID: 0", (), source)
        return next((c.detail for c in result.errors if c.id == "squashfs.required"), "")

    assert "usr/bin/gentoo-chroot" not in missing(lambda p: False)
    assert "usr/bin/gentoo-chroot" in missing(lambda p: p == "src/gentoo_installerd/rescue/__init__.py")


def test_required_modes_are_checked() -> None:
    """/mnt/rescue holds the repair sessions: a 0755 directory (the fsscript's old mode) must not pass."""
    def wrong(mode: str) -> str:
        listing = LISTING + f"dir mnt/rescue {mode} 0 0\n"
        result = verify.Report()
        verify.check_squashfs(result, verify.parse_description(listing), CONTRACT, [], "testkey", lambda p: b"", "UID: 0\nGID: 0", (), lambda p: False)
        return next((c.detail for c in result.errors if c.id == "squashfs.required_types"), "")

    assert "mnt/rescue" not in wrong("0700")
    assert "mnt/rescue has mode 0755, expected 0700" in wrong("0755")
    assert "mnt/rescue has mode 0777, expected 0700" in wrong("0777")


def test_package_rules() -> None:
    report = squashfs_report(LISTING)
    required = next(c.detail for c in report.errors if c.id == "squashfs.required_packages")
    assert "sys-apps/memtest86+" in required and "sys-fs/lvm2" not in required
    assert "dev-lang/rust-bin" in next(c.detail for c in report.errors if c.id == "squashfs.forbidden_packages")


def test_root_directory_must_belong_to_root() -> None:
    report = verify.Report()
    verify.check_squashfs(report, verify.parse_description(LISTING), CONTRACT, [], "testkey", lambda p: b"", "UID: 1000 (index = 1)\nGID: 1000 (index = 1)", (), lambda p: False)
    assert "squashfs.root_owner" in failed(report)


# --- initramfs -----------------------------------------------------------------------------


def cpio(names: list[str]) -> bytes:
    """A newc archive with empty files; enough for the name check."""
    out = io.BytesIO()
    for index, name in enumerate(names + ["TRAILER!!!"], 1):
        encoded = name.encode() + b"\0"
        header = b"070701" + b"".join(b"%08X" % value for value in (
            index, 0o100644, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(encoded), 0))
        out.write(header + encoded)
        out.write(b"\0" * (-(110 + len(encoded)) % 4))
    return out.getvalue()


def initramfs(names: list[str]) -> bytes:
    early = cpio(["kernel/x86/microcode/AuthenticAMD.bin"])
    early += b"\0" * (-len(early) % 512)
    return early + lzma.compress(cpio(names), format=lzma.FORMAT_XZ)


GOOD_INITRAMFS = [
    *CONTRACT["initramfs"]["required"],
    *[f"usr/lib/modules/6.18/kernel/{m}.ko" for m in ("fs/isofs/isofs", "fs/squashfs/squashfs", "fs/overlayfs/overlay", "drivers/block/loop",
                                                    "drivers/usb/storage/usb-storage", "drivers/usb/storage/uas", "drivers/pci/controller/vmd",
                                                    "drivers/mmc/host/sdhci-pci", "drivers/mmc/core/mmc_block", "drivers/md/dm-crypt", "drivers/md/raid1")],
    *[f"usr/lib/keymaps/{n}.map" for n in range(30)],
]


def test_initramfs_names_reads_early_and_main_archives() -> None:
    names = verify.initramfs_names(initramfs(["a/b", "c"]))
    assert names == ["kernel/x86/microcode/AuthenticAMD.bin", "a/b", "c"]


def test_initramfs_of_the_accepted_layout_passes() -> None:
    report = verify.Report()
    verify.check_initramfs(report, verify.initramfs_names(initramfs(GOOD_INITRAMFS)), CONTRACT, has_dokeymap=True)
    assert report.ok, [c.detail for c in report.errors]


def test_initramfs_without_dmsquash_live_or_storage_modules_fails() -> None:
    report = verify.Report()
    names = [n for n in GOOD_INITRAMFS if "dmsquash-live-root" not in n and "usb-storage" not in n]
    verify.check_initramfs(report, names, CONTRACT, has_dokeymap=True)
    assert {"initramfs.files", "initramfs.modules"} <= failed(report)


def test_keymaps_are_required_only_when_a_menu_entry_asks_for_them() -> None:
    names = [n for n in GOOD_INITRAMFS if "keymaps" not in n]
    quiet, asking = verify.Report(), verify.Report()
    verify.check_initramfs(quiet, names, CONTRACT, has_dokeymap=False)
    verify.check_initramfs(asking, names, CONTRACT, has_dokeymap=True)
    assert "initramfs.keymaps" not in failed(quiet) and "initramfs.keymaps" in failed(asking)


def test_the_real_initramfs_format_is_understood() -> None:
    with pytest.raises(verify.VerifyError):
        verify.initramfs_names(b"not an initramfs")


# --- end to end with real tools ------------------------------------------------------------

TOOLS = ("xorriso", "isoinfo", "rdsquashfs", "gensquashfs")


def tools_missing() -> bool:
    return any(shutil.which(t) is None for t in TOOLS)


def write_pack_file(tree: dict[str, tuple[str, int, int, int, str]], path: Path, source: Path) -> None:
    """tree: path -> (kind, mode, uid, gid, content-or-target)."""
    lines = []
    for name, (kind, mode, uid, gid, extra) in sorted(tree.items()):
        if kind == "dir":
            lines.append(f"dir /{name} {mode:o} {uid} {gid}")
        elif kind == "slink":
            lines.append(f"slink /{name} {mode:o} {uid} {gid} {extra}")
        else:
            data = source / name.replace("/", "__")
            data.write_text(extra)
            lines.append(f"file /{name} {mode:o} {uid} {gid} {data}")
    path.write_text("\n".join(lines) + "\n")


def source_applies(condition: str | dict[str, str]) -> bool:
    """The contract's when_source rule against this repository: what the ebuild would install."""
    if isinstance(condition, str):
        return (ROOT / condition).exists()
    path = ROOT / condition["path"]
    return path.is_file() and condition["contains"] in path.read_text()


# The accounts of the synthetic image: what Catalyst's livecdfs-update and the fsscript leave behind.
PASSWD = "root:x:0:0:root:/root:/bin/bash\ngentoo:x:1000:100:Live user:/home/gentoo:/bin/bash\nrescue-ai:x:999:999:AI rescue agents:/home/rescue-ai:/bin/bash\n"
GROUP = "root:x:0:root\nwheel:x:10:gentoo\ndisk:x:6:gentoo\ntty:x:5:\nusers:x:100:\nrescue-ai:x:999:\n"
SUDOERS = "root ALL=(ALL:ALL) ALL\n%wheel ALL=(ALL:ALL) NOPASSWD: ALL\n@includedir /etc/sudoers.d\n"


def image_entries(flavor: str, live_make_conf: str) -> dict[str, tuple[str, int, int, int, str]]:
    """A root file system that satisfies iso/image-contract.json, built from the repository's own overlay."""
    entries: dict[str, tuple[str, int, int, int, str]] = {}

    def add_dirs(path: str) -> None:
        parts = path.split("/")
        for i in range(1, len(parts)):
            entries.setdefault("/".join(parts[:i]), ("dir", 0o755, 0, 0, ""))

    def add(path: str, kind: str = "file", mode: int = 0o644, extra: str = "", uid: int = 0, gid: int = 0) -> None:
        add_dirs(path)
        entries[path] = (kind, mode, uid, gid, extra)

    overlay = ROOT / "iso/root_overlay"
    for file in sorted(p for p in overlay.rglob("*") if p.is_file()):
        relative = str(file.relative_to(overlay))
        add(relative, mode=0o755 if os.access(file, os.X_OK) else 0o644, extra=file.read_text())
    spec = verify.parse_spec((ROOT / "iso/specs/livecd-stage2.spec").read_text())
    services = verify.spec_list(spec, "livecd/rcadd") + [f"{s}|{l}" for s, l in CONTRACT["squashfs"]["implicit_services"]]
    for item in services:
        service, _, level = item.partition("|")
        add(f"etc/init.d/{service}", mode=0o755, extra="#!/sbin/openrc-run\n")
        add(f"etc/runlevels/{level}/{service}", "slink", 0o777, f"/etc/init.d/{service}")
    for item in CONTRACT["squashfs"]["required"]:
        if "when_source" in item and not source_applies(item["when_source"]):
            continue
        path = item["path"]
        if path not in entries:
            kind = item.get("type", "file").split("|")[0]
            mode = int(item["mode"], 8) if item.get("mode") else 0o755 if item.get("executable") or kind == "dir" else 0o644
            add(path, kind, mode, extra="/target" if kind == "slink" else "")
    for command in CONTRACT["squashfs"]["commands"]:
        add(f"usr/bin/{command}", mode=0o755)
    for package in CONTRACT["squashfs"]["required_packages"]:
        category, name = package.split("/")
        add(f"var/db/pkg/{category}/{name}-1.0", "dir", 0o755)
    add("etc/portage/make.conf", extra=live_make_conf)
    add("etc/shadow", mode=0o640, extra="root:!:20000::::::\ngentoo::20000:0:99999:7:::\nrescue-ai:!:20000::::::\n")
    # what app-admin/gentoo-live-installer[rescue-ai] installs from its files/ directory
    add("etc/init.d/gentoo-rescue-approver", mode=0o755, extra=(FILES_DIR / "gentoo-rescue-approver.initd").read_text())
    add("etc/pam.d/su", mode=0o644, extra="auth\t\tsufficient\tpam_rootok.so\nauth\t\trequired\tpam_wheel.so use_uid\nauth\t\tinclude\t\tsystem-auth\n")
    add("etc/conf.d/gentoo-rescue-approver", extra=(FILES_DIR / "gentoo-rescue-approver.confd").read_text())
    add("etc/passwd", extra=PASSWD)
    add("etc/group", extra=GROUP)
    add("etc/sudoers", mode=0o440, extra=SUDOERS)
    add("home/gentoo/.bashrc", uid=1000, gid=100)
    entries["home/gentoo"] = ("dir", 0o700, 1000, 100, "")
    for banner in ("etc/issue", "etc/motd"):
        text = entries[banner][4]
        if flavor == "testkey":
            text = "TEST BUILD\n" + text
        add(banner, extra=text)
    if flavor == "testkey":
        add("root/.ssh/authorized_keys", mode=0o600, extra="ssh-ed25519 AAAA\n")
        add("etc/gentoo-installer-vm-test")
        add("etc/ssh/sshd_config.d/90-installer-vm-test.conf", extra="PermitRootLogin prohibit-password\n")
    return entries


@pytest.fixture()
def live_make_conf(tmp_path: Path, pins: dict[str, str]) -> str:
    values = render.token_values(pins, str(tmp_path), "4", "8")
    template = render.render_text((ROOT / "iso/portage/make.conf").read_text(), values)
    assignments = render.extract_assignments(template)
    live = {k: v for k, v in assignments.items() if k in wrapper_keys.LIVE_KEYS}
    live["FEATURES"] = " ".join(w for w in live["FEATURES"].split() if w != "buildpkg")
    return "".join(f'{k}="{v}"\n' for k, v in live.items())


FILES_DIR = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files"
KERNEL_CONFIG = (FILES_DIR / "live-safe-graphics.config").read_text() + (FILES_DIR / "live-rescue-ai.config").read_text() + "CONFIG_DRM_NOUVEAU=m\n# CONFIG_DRM_EFIDRM is not set\n"


def build_iso(tmp_path: Path, flavor: str, live_make_conf: str, pins: dict[str, str], *, mutate=None) -> Path:
    work = tmp_path / "iso-work"
    tree = work / "tree"
    (tree / "boot/grub/i386-pc").mkdir(parents=True)
    (tree / "boot/grub/x86_64-efi").mkdir()
    (tree / "boot/grub/i386-efi").mkdir()
    (tree / "boot/grub/themes/gentoo_frosted").mkdir(parents=True)
    (tree / "efi/boot").mkdir(parents=True)
    volid = verify.flavor_volume_id(pins["ISO_VOLID"], flavor)
    env = dict(pins, ISO_VOLID=volid)
    values = render.token_values(env, str(work), "2", "4")
    (tree / "boot/grub/grub.cfg").write_text(render.render_text((ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text(), values))
    (tree / "boot/gentoo").write_bytes(b"kernel")
    (tree / "boot/gentoo-config").write_text(KERNEL_CONFIG)
    (tree / "boot/gentoo.igz").write_bytes(initramfs(GOOD_INITRAMFS))
    (tree / "boot/grub/i386-pc/eltorito.img").write_bytes(b"\0" * 2048)
    (tree / "boot/grub/x86_64-efi/a.mod").write_bytes(b"x")
    (tree / "boot/grub/i386-efi/a.mod").write_bytes(b"x")
    (tree / "boot/grub/themes/gentoo_frosted/theme.txt").write_text("x")
    for name in ("efi/boot/bootx64.efi", "efi/boot/bootia32.efi", "efi/boot/grubx64.efi", "efi/boot/mmx64.efi", "memtest64.bios", "memtest.efi64",
                 "livecd", "ENROLL_THIS_KEY_IN_MOKMANAGER.cer"):
        (tree / name).write_bytes(b"x")
    shutil.copy(ROOT / "iso/cd_overlay/SECURE-BOOT.txt", tree / "SECURE-BOOT.txt")
    (tree / "README.txt").write_text("readme")
    entries = image_entries(flavor, live_make_conf)
    if mutate:
        mutate(entries, tree)
    pack = work / "pack.txt"
    data = work / "data"
    data.mkdir()
    write_pack_file(entries, pack, data)
    subprocess.run(["gensquashfs", "-F", str(pack), "-q", "-f", "-c", "gzip", str(tree / "image.squashfs")], check=True, capture_output=True)
    (tree / "efi.img").write_bytes(b"\0" * 65536)
    (work / "mbr.bin").write_bytes(b"\0" * 512)
    iso = tmp_path / "test.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-V", volid, "-R", "-J", "-graft-points",
         "-b", "/boot/grub/i386-pc/eltorito.img", "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
         "--grub2-mbr", str(work / "mbr.bin"), "-eltorito-alt-boot", "-e", "/efi.img", "-no-emul-boot",
         "-append_partition", "2", "0xef", str(tree / "efi.img"), "-appended_part_as_gpt", "-iso_mbr_part_type", "0x83",
         "-o", str(iso), str(tree)],
        check=True, capture_output=True,
    )
    return iso


# What Tools.secure_boot_report finds on a real signed ISO (iso/sign-secure-boot.py, checked in the builder image, which has sbverify):
# the synthetic image has no shim, signed GRUB or kernel, so its Secure Boot part comes from here.
GOOD_SECURE_BOOT = {
    "efi_files": ["EFI/BOOT/BOOTIA32.EFI", "EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi", "EFI/BOOT/mmx64.efi", "ENROLL_THIS_KEY_IN_MOKMANAGER.cer"],
    "certificate_matches": True,
    "certificate_subject": "subject=CN=Gentoo installer live disc x.iso 2026-10-27 04:19 UTC",
    "shim_signers": " - /C=US/ST=Washington/L=Redmond/O=Microsoft Corporation/CN=Microsoft Corporation UEFI CA 2011\n"
                    " - /C=US/O=Microsoft Corporation/CN=Microsoft UEFI CA 2023\n",
    "verified": {"EFI/BOOT/grubx64.efi": True, "/boot/gentoo": True, "/efi/boot/grubx64.efi": True},
    "grub_sbat": "sbat,1,SBAT Version,sbat,1,https://github.com/rhboot/shim/blob/main/SBAT.md\ngrub,5,Free Software Foundation,grub,2.14,https://www.gnu.org/software/grub/\n",
    "copies": {"/efi/boot/bootx64.efi": True, "/efi/boot/mmx64.efi": True, "/efi/boot/grubx64.efi": True},
}


class FixtureTools(verify.Tools):
    def secure_boot_report(self, spec):
        return copy.deepcopy(GOOD_SECURE_BOOT)


def secure_boot_failures(**changes) -> set[str]:
    found = copy.deepcopy(GOOD_SECURE_BOOT)
    found.update(changes)
    report = verify.Report()
    verify.check_secure_boot(report, found, CONTRACT["secure_boot"])
    return {check.id for check in report.checks if not check.ok}


def test_a_signed_disc_passes_the_secure_boot_checks() -> None:
    assert secure_boot_failures() == set()


@pytest.mark.parametrize("changes, failure", [
    ({"efi_files": ["EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grubx64.efi"]}, "secure_boot.efi_files"),  # no MokManager: nobody could enroll the key
    ({"certificate_matches": False}, "secure_boot.certificate"),
    ({"shim_signers": " - /CN=Fedora Secure Boot CA\n"}, "secure_boot.shim"),
    ({"shim_signers": " - /CN=Microsoft Corporation UEFI CA 2011\n"}, "secure_boot.shim_both_cas"),
    ({"verified": {"EFI/BOOT/grubx64.efi": True, "/boot/gentoo": False, "/efi/boot/grubx64.efi": True}}, "secure_boot.signed"),
    ({"verified": {"EFI/BOOT/grubx64.efi": True}}, "secure_boot.signed"),  # a file that was never checked is not a signed file
    ({"grub_sbat": "sbat,1,SBAT Version\ngrub,4,Free Software Foundation\n"}, "secure_boot.sbat"),  # shim revoked generation 4
    ({"grub_sbat": ""}, "secure_boot.sbat"),
    ({"copies": {"/efi/boot/bootx64.efi": False, "/efi/boot/mmx64.efi": True, "/efi/boot/grubx64.efi": True}}, "secure_boot.iso_tree"),
])
def test_each_broken_link_of_the_chain_is_named(changes, failure) -> None:
    assert secure_boot_failures(**changes) == {failure}


def test_only_one_microsoft_ca_is_a_warning_not_an_error() -> None:
    found = dict(GOOD_SECURE_BOOT, shim_signers=" - /CN=Microsoft UEFI CA 2023\n")
    report = verify.Report()
    verify.check_secure_boot(report, found, CONTRACT["secure_boot"])
    assert report.ok and [c.id for c in report.warnings] == ["secure_boot.shim_both_cas"]


@pytest.mark.skipif(tools_missing(), reason="xorriso, isoinfo, gensquashfs and rdsquashfs are needed")
@pytest.mark.parametrize("flavor", ["release", "testkey"])
def test_a_synthetic_image_built_from_the_overlay_satisfies_the_contract(tmp_path: Path, live_make_conf: str, pins: dict[str, str], flavor: str) -> None:
    """The overlay, the specs, the menu template and the contract agree with each other."""
    iso = build_iso(tmp_path, flavor, live_make_conf, pins)
    tools = FixtureTools(iso, tmp_path / "scratch")
    # The repair tool's own files are only required when the source tree contains it.
    report, packages = verify.verify(iso, flavor=flavor, workspace=ROOT, tools=tools)
    assert report.ok, "\n".join(c.id + ": " + c.detail for c in report.errors)
    assert "sys-fs/lvm2-1.0" in packages


@pytest.mark.skipif(tools_missing(), reason="xorriso, isoinfo, gensquashfs and rdsquashfs are needed")
def test_the_same_image_fails_when_something_is_taken_away(tmp_path: Path, live_make_conf: str, pins: dict[str, str]) -> None:
    def break_it(entries, tree):
        del entries["etc/runlevels/default/chronyd"]
        entries["etc/lvm/lvmlocal.conf"] = ("file", 0o644, 1000, 1000, "global {}\n")
        (tree / "boot/grub/grub.cfg").write_text((FIXTURES / "grub_r19.cfg").read_text())

    iso = build_iso(tmp_path, "release", live_make_conf, pins, mutate=break_it)
    report, _ = verify.verify(iso, flavor="release", workspace=ROOT, tools=FixtureTools(iso, tmp_path / "scratch"))
    ids = failed(report)
    assert {"squashfs.services", "squashfs.ownership", "grub.entries", "squashfs.content"} <= ids


@pytest.mark.skipif(tools_missing(), reason="xorriso, isoinfo, gensquashfs and rdsquashfs are needed")
def contract_without_secure_boot(tmp_path: Path) -> Path:
    """The command line on the synthetic image: this host has no sbverify, and the image no signed files (the checks above cover them)."""
    contract = {key: value for key, value in CONTRACT.items() if key != "secure_boot"}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    return path


def test_command_line_report_and_package_list(tmp_path: Path, live_make_conf: str, pins: dict[str, str]) -> None:
    iso = build_iso(tmp_path, "release", live_make_conf, pins)
    report_path, packages_path = tmp_path / "report.json", tmp_path / "packages.txt"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify-iso.py"), str(iso), "--json", str(report_path), "--packages-out", str(packages_path),
         "--workdir", str(tmp_path / "w"), "--contract", str(contract_without_secure_boot(tmp_path))],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report_path.read_text())
    assert data["ok"] is True and data["flavor"] == "release" and data["errors"] == []
    assert "sys-fs/lvm2-1.0" in packages_path.read_text().split()
    assert not (tmp_path / "w/image.squashfs").exists(), "the 3 GiB copy of the image must be removed"
    wrong = subprocess.run([sys.executable, str(ROOT / "scripts/verify-iso.py"), str(iso), "--flavor", "testkey", "--workdir", str(tmp_path / "w2"),
                            "--contract", str(contract_without_secure_boot(tmp_path))],
                           capture_output=True, text=True, check=False)
    assert wrong.returncode == 1 and "FAIL" in wrong.stdout
    assert subprocess.run([sys.executable, str(ROOT / "scripts/verify-iso.py"), str(tmp_path / "missing.iso")], capture_output=True, check=False).returncode == 2


def test_previous_manifest_is_only_a_hint(tmp_path: Path) -> None:
    """A missing, unreadable or foreign manifest must never fail a build over a size warning."""
    manifest = tmp_path / "m.json"
    assert verify.previous_iso_size(None) is None
    assert verify.previous_iso_size(tmp_path / "absent.json") is None
    for text, expected in (
        ('{"iso": {"size": 3000000000}}', 3_000_000_000),
        ("{not json", None), ("[]", None), ('"text"', None), ("{}", None),
        ('{"iso": "x"}', None), ('{"iso": {"size": "3"}}', None), ('{"iso": {"size": true}}', None), ('{"iso": {"size": 0}}', None),
    ):
        manifest.write_text(text)
        assert verify.previous_iso_size(manifest) == expected, text


@pytest.mark.skipif(tools_missing(), reason="xorriso, isoinfo, gensquashfs and rdsquashfs are needed")
def test_growth_over_the_previous_build_is_a_warning(tmp_path: Path, live_make_conf: str, pins: dict[str, str]) -> None:
    iso = build_iso(tmp_path, "release", live_make_conf, pins)
    previous = tmp_path / "previous.manifest.json"

    def run(previous_size: int) -> subprocess.CompletedProcess:
        previous.write_text(json.dumps({"iso": {"size": previous_size}}))
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/verify-iso.py"), str(iso), "--previous-manifest", str(previous), "--workdir", str(tmp_path / "w"),
             "--contract", str(contract_without_secure_boot(tmp_path))],
            capture_output=True, text=True, check=False,
        )

    grown = run(1000)
    assert grown.returncode == 0, grown.stdout + grown.stderr
    assert "[WARN] iso.size" in grown.stdout and "1 warnings" in grown.stdout
    same = run(iso.stat().st_size)
    assert same.returncode == 0 and "[ok  ] iso.size" in same.stdout and "0 warnings" in same.stdout


# --- I9: sshd on every flavour, no host keys, the first value in name order -------------------------

SSH_LISTING = (
    "dir etc/ssh 0755 0 0\ndir etc/ssh/sshd_config.d 0755 0 0\n"
    "file etc/ssh/sshd_config.d/10-gentoo-live.conf 0644 0 0\n"
    "file etc/ssh/sshd_config.d/9999999gentoo-livecd.conf 0644 0 0\n"
    "file etc/ssh/sshd_config.d/9999999gentoo-pam.conf 0644 0 0\n"
)
SSH_FILES = {
    "/etc/ssh/sshd_config.d/10-gentoo-live.conf": "# live\nPermitEmptyPasswords no\n",
    "/etc/ssh/sshd_config.d/9999999gentoo-livecd.conf": "PermitRootLogin Yes\n",
    "/etc/ssh/sshd_config.d/9999999gentoo-pam.conf": "UsePAM yes\nPasswordAuthentication no\n",
}


def sshd_failed(listing: str, files: dict[str, str], flavor: str = "release") -> bool:
    shadow = {"/etc/shadow": "root:!:20000::::::\n"}
    return "squashfs.sshd" in failed(squashfs_report(LISTING + listing, flavor=flavor, files={**shadow, **files}))


def test_release_sshd_refuses_empty_passwords_first() -> None:
    assert not sshd_failed(SSH_LISTING, SSH_FILES)
    # a drop-in that sorts before and allows empty passwords wins over ours
    early = SSH_LISTING + "file etc/ssh/sshd_config.d/05-open.conf 0644 0 0\n"
    assert sshd_failed(early, {**SSH_FILES, "/etc/ssh/sshd_config.d/05-open.conf": "PermitEmptyPasswords yes\n"})
    # no drop-in sets it at all
    assert sshd_failed(SSH_LISTING, {**SSH_FILES, "/etc/ssh/sshd_config.d/10-gentoo-live.conf": "# nothing\n"})
    # a setting inside a Match block is not the global one
    assert sshd_failed(SSH_LISTING, {**SSH_FILES, "/etc/ssh/sshd_config.d/10-gentoo-live.conf": "Match User x\nPermitEmptyPasswords no\n"})
    assert sshd_failed("", {})  # no drop-in directory in the image


def test_testkey_sshd_needs_the_key_only_root_login_first() -> None:
    files = {**SSH_FILES, "/etc/ssh/sshd_config.d/90-installer-vm-test.conf": "PermitRootLogin prohibit-password\n"}
    listing = SSH_LISTING + "file etc/ssh/sshd_config.d/90-installer-vm-test.conf 0644 0 0\n"
    assert not sshd_failed(listing, files, "testkey")
    # without the test drop-in the live package's PermitRootLogin Yes would be the first value
    assert sshd_failed(SSH_LISTING, SSH_FILES, "testkey")


def test_sshd_first_value_reads_in_name_order_and_ignores_comments() -> None:
    entries = verify.parse_description(SSH_LISTING + "file etc/ssh/sshd_config.d/00-a.conf 0644 0 0\n")
    files = {**SSH_FILES, "/etc/ssh/sshd_config.d/00-a.conf": "# PermitEmptyPasswords yes\n  permitemptypasswords no # why\n"}
    value, where = verify.sshd_first_value(entries, lambda path: files[path].encode(), "PermitEmptyPasswords")
    assert (value, where) == ("no", "etc/ssh/sshd_config.d/00-a.conf")
    assert verify.sshd_first_value(entries, lambda path: files[path].encode(), "PermitRootLogin")[0] == "Yes"


def test_the_contract_requires_sshd_everywhere_and_no_host_keys() -> None:
    contract = json.loads((ROOT / "iso/image-contract.json").read_text())
    assert not [p for p in contract["flavor"]["testkey_required"] if p.endswith("/sshd")]
    assert "etc/ssh/ssh_host_*" in contract["squashfs"]["forbidden"]
    assert contract["squashfs"]["sshd_first_values"]["release"] == {"PermitEmptyPasswords": "no"}
    assert "sshd|default" in verify.spec_list(verify.parse_spec((ROOT / "iso/specs/livecd-stage2.spec").read_text()), "livecd/rcadd")
    report = squashfs_report(LISTING + SSH_LISTING + "file etc/ssh/ssh_host_rsa_key.pub 0644 0 0\n", files=SSH_FILES)
    assert "squashfs.forbidden" in failed(report)


@pytest.mark.skipif(tools_missing(), reason="xorriso, isoinfo, gensquashfs and rdsquashfs are needed")
def test_an_image_whose_kernel_has_no_generic_drm_driver_fails(tmp_path: Path, live_make_conf: str, pins: dict[str, str]) -> None:
    """H1: the kernel of gentoo-kernel-bin cannot show a Safe graphics desktop."""
    def binary_kernel(entries, tree):
        (tree / "boot/gentoo-config").write_text("# CONFIG_SYSFB_SIMPLEFB is not set\n# CONFIG_DRM_SIMPLEDRM is not set\nCONFIG_FB_EFI=y\n" + (FILES_DIR / "live-rescue-ai.config").read_text())

    iso = build_iso(tmp_path, "release", live_make_conf, pins, mutate=binary_kernel)
    report, _ = verify.verify(iso, flavor="release", workspace=ROOT, tools=FixtureTools(iso, tmp_path / "scratch"))
    assert failed(report) == {"kernel.generic_drm"}

    def no_config(entries, tree):
        (tree / "boot/gentoo-config").unlink()

    iso = build_iso(tmp_path / "second", "release", live_make_conf, pins, mutate=no_config)
    report, _ = verify.verify(iso, flavor="release", workspace=ROOT, tools=FixtureTools(iso, tmp_path / "scratch2"))
    assert {"iso.required_files", "kernel.config"} <= failed(report)
