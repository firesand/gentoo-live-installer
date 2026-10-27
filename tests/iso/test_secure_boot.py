"""iso/sign-secure-boot.py: the live disc starts with Secure Boot on (shim, a signed GRUB and kernel, a key for one ISO only).

The real tools run in the builder image (the VM test secure-boot-live boots the result); here a fake runner records what the signer asks
for, so that the rules that matter can be checked without them: the private key never leaves the secret directory and is destroyed even
when a step fails, the boot setup is written again without "replay", and every module the boot menu loads is built into GRUB.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest

from .conftest import ROOT, load_script

sign = load_script("iso/sign-secure-boot.py", "sign_secure_boot")
MARKER = "/.disk/2026-10-27-02-39-39-00.uuid"
DATE = "2026102702393900"
ORIGINAL_BOOT = ("-V 'GENTOO_INSTALLER_AMD64'\n--modification-date='2026102702393900'\n--grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt:'x.iso'\n"
                 "--protective-msdos-label\n-efi-boot-part --efi-boot-image\n-c '/boot.catalog'\n-b '/boot/grub/i386-pc/eltorito.img'\n"
                 "-no-emul-boot\n-boot-load-size 4\n-boot-info-table\n--grub2-boot-info\n-eltorito-alt-boot\n-e '/efi.img'\n-no-emul-boot\n")
REWRITTEN_AREA = "GPT partition path :   2  /efi.img\n"


class FakeTools:
    """Answers the signer's commands like the real tools would and keeps every command line."""

    def __init__(self, tmp_path: Path, rewritten_report: str = ORIGINAL_BOOT + REWRITTEN_AREA) -> None:
        self.calls: list[list[str]] = []
        self.rewritten_report = rewritten_report

    def __call__(self, *command) -> str:
        argv = [str(part) for part in command]
        self.calls.append(argv)
        tool = argv[0]
        if tool == "xorriso":
            if "-find" in argv:
                return f"'{MARKER}'\n"
            if "-pvd_info" in argv:
                return f"Volume Id    : GENTOO_INSTALLER_AMD64\nModif. Time  : {DATE}\n"
            if "-osirrox" in argv:
                for index, word in enumerate(argv):
                    if word == "-extract":
                        Path(argv[index + 2]).write_bytes(b"kernel" if argv[index + 1] == "/boot/gentoo" else b"catalyst efi.img")
                return ""
            if "-outdev" in argv:
                Path(argv[argv.index("-outdev") + 1]).write_bytes(b"rewritten iso")
                return ""
            if "-report_el_torito" in argv:
                indev = argv[argv.index("-indev") + 1]
                return self.rewritten_report if indev.endswith(".secure-boot") else ORIGINAL_BOOT
        if tool == "mcopy" and argv[-2].startswith("::/"):
            Path(argv[-1]).write_bytes(b"32-bit grub")
        if tool == "grub-mkimage":
            Path(argv[argv.index("-o") + 1]).write_bytes(b"MZ grub .sbat sbat,1,SBAT Version\ngrub,5,Free Software Foundation\n")
        if tool == "openssl" and "req" in argv:
            Path(argv[argv.index("-keyout") + 1]).write_text("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n")
            Path(argv[argv.index("-out") + 1]).write_text("-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n")
        if tool == "openssl" and "-outform" in argv:
            Path(argv[argv.index("-out") + 1]).write_bytes(b"0\x82 DER certificate")
        if tool == "sbsign":
            Path(argv[argv.index("--output") + 1]).write_bytes(Path(argv[-1]).read_bytes() + b" signed")
        if tool == "mkfs.vfat":
            Path(argv[-2]).write_bytes(b"fat")
        return ""

    def named(self, tool: str) -> list[list[str]]:
        return [argv for argv in self.calls if argv[0] == tool]


@pytest.fixture
def signing(tmp_path, monkeypatch):
    shim = tmp_path / "shim"
    grub = tmp_path / "grub/x86_64-efi"
    shim.mkdir()
    grub.mkdir(parents=True)
    for name in ("BOOTX64.EFI", "mmx64.efi"):
        (shim / name).write_bytes(b"MZ shim " + name.encode())
    for module in sign.GRUB_MODULES:
        (grub / f"{module}.mod").write_bytes(b"module")
    (tmp_path / "sbat.csv").write_text("sbat,1\ngrub,5\n")
    secrets = tmp_path / "shm"
    secrets.mkdir()
    made: list[Path] = []

    def secret_directory() -> Path:
        directory = secrets / f"key{len(made)}"
        directory.mkdir()
        made.append(directory)
        return directory

    monkeypatch.setattr(sign, "secret_directory", secret_directory)
    iso = tmp_path / "dist/gentoo.iso"
    iso.parent.mkdir()
    iso.write_bytes(b"catalyst iso")
    tools = sign.Tools(shim_dir=shim, grub_dir=grub, sbat=tmp_path / "sbat.csv")
    return tmp_path, iso, tools, secrets, made


def every_file(*roots: Path) -> list[Path]:
    return [path for root in roots for path in root.rglob("*") if path.is_file()]


def test_the_disc_is_signed_and_the_private_key_is_gone_afterwards(signing, monkeypatch) -> None:
    tmp_path, iso, tools, secrets, made = signing
    fake = FakeTools(tmp_path)
    monkeypatch.setattr(sign, "run", fake)
    summary = sign.sign_iso(iso, tmp_path / "work", tmp_path / "dist/gentoo.iso.secureboot.cer", tools)

    assert iso.read_bytes() == b"rewritten iso"
    assert made and not any(directory.exists() for directory in made) and list(secrets.iterdir()) == []
    leaked = [path for path in every_file(tmp_path / "work", tmp_path / "dist") if b"PRIVATE KEY" in path.read_bytes()]
    assert leaked == [], "the private key must exist only in the secret directory"
    # the key signed GRUB and the kernel, and both were verified with the certificate
    signed = [argv[-1] for argv in fake.named("sbsign")]
    assert [Path(path).name for path in signed] == ["grubx64.unsigned.efi", "gentoo"]
    assert len(fake.named("sbverify")) == 2
    assert summary["certificate_sha256"] == hashlib.sha256(b"0\x82 DER certificate").hexdigest()
    assert summary["mokmanager_fingerprint"] == sign.mokmanager_fingerprint(b"0\x82 DER certificate")
    assert summary["marker"] == MARKER and summary["subject"] == sign.certificate_subject(summary["subject_date"])


def test_the_key_name_fits_a_certificate_whatever_the_iso_is_called(signing, monkeypatch) -> None:
    # RC3's first build: "CN=Gentoo installer live disc <98 characters with the ISO name>" made openssl refuse the key
    tmp_path, iso, tools, _, _ = signing
    long_name = iso.with_name("gentoo-installer-amd64-20261018T124555Z-TESTKEY.iso")
    iso.rename(long_name)
    fake = FakeTools(tmp_path)
    monkeypatch.setattr(sign, "run", fake)
    summary = sign.sign_iso(long_name, tmp_path / "work", tmp_path / "dist/x.secureboot.cer", tools)
    (request,) = [argv for argv in fake.named("openssl") if "req" in argv]
    common_name = request[request.index("-subj") + 1]
    assert common_name == f"/CN={summary['subject']}/" and len(summary["subject"]) <= sign.CN_MAX
    assert long_name.name not in common_name and "TESTKEY" not in common_name


def test_the_key_name_is_short_plain_text() -> None:
    assert sign.certificate_subject("2026-10-27 07:49 UTC") == "Gentoo installer live disc 2026-10-27 07:49 UTC"
    for stamp in ("2026-10-27 07:49 UTC" + "x" * 40, "2026-10-27/CN=evil"):
        with pytest.raises(sign.SignError, match="certificate name"):
            sign.certificate_subject(stamp)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_the_real_openssl_makes_a_code_signing_key_with_that_name(tmp_path) -> None:
    subject = sign.certificate_subject("2026-10-27 07:49 UTC")
    key, pem, der = sign.make_key(tmp_path, f"/CN={subject}/")
    assert key.read_text().startswith("-----BEGIN PRIVATE KEY-----") and der.read_bytes()[:1] == b"0"
    text = subprocess.run(["openssl", "x509", "-noout", "-subject", "-ext", "extendedKeyUsage,keyUsage", "-in", str(pem)],
                          check=True, capture_output=True, text=True).stdout
    assert f"subject=CN={subject}" in text and "Code Signing" in text and "Digital Signature" in text


def test_a_failing_step_after_the_key_was_made_still_destroys_it(signing, monkeypatch) -> None:
    tmp_path, iso, tools, secrets, made = signing
    fake = FakeTools(tmp_path)

    def failing(*command):
        if str(command[0]) == "sbsign":
            raise sign.SignError("sbsign failed (1): no")
        return fake(*command)

    monkeypatch.setattr(sign, "run", failing)
    with pytest.raises(sign.SignError):
        sign.sign_iso(iso, tmp_path / "work", tmp_path / "out.cer", tools)
    assert made and not made[0].exists()
    assert iso.read_bytes() == b"catalyst iso", "the ISO is replaced only when everything worked"


def test_a_rewritten_iso_without_the_usb_partition_is_refused(signing, monkeypatch) -> None:
    tmp_path, iso, tools, _, _ = signing
    monkeypatch.setattr(sign, "run", FakeTools(tmp_path, rewritten_report=ORIGINAL_BOOT))
    with pytest.raises(sign.SignError, match="GPT partition"):
        sign.sign_iso(iso, tmp_path / "work", tmp_path / "out.cer", tools)
    assert iso.read_bytes() == b"catalyst iso"


def test_the_boot_setup_is_written_again_without_replay(signing, monkeypatch) -> None:
    tmp_path, iso, tools, _, _ = signing
    fake = FakeTools(tmp_path)
    monkeypatch.setattr(sign, "run", fake)
    sign.sign_iso(iso, tmp_path / "work", tmp_path / "out.cer", tools)
    (write,) = [argv for argv in fake.named("xorriso") if "-outdev" in argv]
    line = shlex.join(write)
    assert "replay" not in write, "libisofs refuses to replay grub-mkrescue's layout (GPT partitions 1 and 2 overlap)"
    assert "-boot_image any discard -as mkisofs" in line and write[-1] == "--"
    for needle in (f"--modification-date={DATE}", "--grub2-mbr", "-b /boot/grub/i386-pc/eltorito.img", "--grub2-boot-info",
                   "-e /efi.img", "-efi-boot-part --efi-boot-image", "--protective-msdos-label"):
        assert needle in line, needle
    assert "-hfsplus" not in write and "-partition_offset" not in write  # together with cyl_align off they make libisofs fail
    maps = {write[i + 2]: write[i + 1] for i, word in enumerate(write) if word == "-map"}
    assert set(maps) == {"/efi.img", "/boot/gentoo", f"/{sign.CERT_NAME}", "/efi/boot/bootx64.efi", "/efi/boot/mmx64.efi", "/efi/boot/grubx64.efi"}
    assert maps["/efi/boot/bootx64.efi"].endswith("shim/BOOTX64.EFI") and maps["/boot/gentoo"].endswith("gentoo.signed")
    # the EFI image: shim first in the boot path, then MokManager, the signed GRUB, Catalyst's 32-bit GRUB and the certificate
    targets = [argv[-1] for argv in fake.named("mcopy") if argv[-1].startswith("::/")]
    assert targets == ["::/EFI/BOOT/BOOTX64.EFI", "::/EFI/BOOT/mmx64.efi", "::/EFI/BOOT/grubx64.efi", "::/EFI/BOOT/BOOTIA32.EFI", f"::/{sign.CERT_NAME}"]
    (mkfs,) = fake.named("mkfs.vfat")
    assert mkfs[mkfs.index("-n") + 1] == sign.EFI_LABEL


def test_grub_is_standalone_with_sbat_and_finds_this_disc_by_its_marker(signing, monkeypatch) -> None:
    tmp_path, iso, tools, _, _ = signing
    fake = FakeTools(tmp_path)
    monkeypatch.setattr(sign, "run", fake)
    sign.sign_iso(iso, tmp_path / "work", tmp_path / "out.cer", tools)
    (mkimage,) = fake.named("grub-mkimage")
    assert mkimage[mkimage.index("-O") + 1] == "x86_64-efi" and mkimage[mkimage.index("--sbat") + 1] == str(tools.sbat)
    assert mkimage[-len(sign.GRUB_MODULES):] == list(sign.GRUB_MODULES)
    assert (tmp_path / "work/early.cfg").read_text() == (f"search --no-floppy --set=root --file {MARKER}\nset prefix=($root)/boot/grub\n"
                                                         "configfile $prefix/grub.cfg\n")


def test_every_module_the_boot_menu_loads_is_built_into_the_signed_grub() -> None:
    """With Secure Boot on, GRUB under shim refuses to load modules from the disc ("prohibited by secure boot policy")."""
    menu = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    loaded = set(re.findall(r"^\s*insmod\s+(\S+)", menu, re.M))
    assert loaded and loaded <= set(sign.GRUB_MODULES), loaded - set(sign.GRUB_MODULES)
    for command, module in (("search ", "search"), ("linux ", "linux"), ("chainloader ", "chain"), ("configfile", "configfile")):
        if command in menu or command == "configfile":
            assert module in sign.GRUB_MODULES


def test_unsigned_memtest_is_hidden_under_lockdown() -> None:
    menu = (ROOT / "iso/cd_overlay/boot/grub/grub.cfg.in").read_text()
    efi = menu[menu.index('if [ "x$grub_platform" = xefi ]; then'):]
    assert efi.index('if [ "x$lockdown" != xy ]; then') < efi.index("chainloader \"/memtest.efi64\"")


@pytest.mark.parametrize("listing, problem", [("", "found []"), (f"'{MARKER}'\n'/.disk/2025-01-01-00-00-00-00.uuid'\n", "found ['/.disk/2025")])
def test_the_marker_must_be_unique(listing, problem) -> None:
    with pytest.raises(sign.SignError, match=re.escape(problem)):
        sign.marker_file(listing)


def test_small_helpers() -> None:
    assert sign.marker_file(f"xorriso : NOTE : ...\n'{MARKER}'\n") == MARKER
    assert sign.volume_date(f"Creation Time: {DATE}\nModif. Time  : {DATE}\n") == DATE
    with pytest.raises(sign.SignError):
        sign.early_config("/boot/grub/grub.cfg; insmod evil")
    assert sign.efi_image_kib([1036008, 874360, 1107632, 167936, 909]) == 5120  # the real files: 5 MiB, as in the VM test
    assert sign.efi_image_kib([]) == 1024
    digest = hashlib.sha1(b"x").hexdigest().upper()
    assert sign.mokmanager_fingerprint(b"x") == " ".join(digest[i:i + 2] for i in range(0, 40, 2))


def test_the_published_notice_shows_the_fingerprint_like_mokmanager() -> None:
    summary = {"mokmanager_fingerprint": "D1 AB AA 1A DC 0D 6C 9C F3 EB 66 72 A3 30 3E DF 31 76 37 98", "subject": "Gentoo installer live disc 2026-10-27 04:19 UTC",
               "certificate_sha256": "ab" * 32}
    text = sign.notice("x.iso", summary)
    assert "[Fingerprint]  D1 AB AA 1A DC 0D 6C 9C F3 EB\n                 66 72 A3 30 3E DF 31 76 37 98\n" in text
    assert text.startswith("Secure Boot key of x.iso\n") and "CN=Gentoo installer live disc 2026-10-27 04:19 UTC\n" in text
    assert "destroyed" in text


def test_the_build_signs_before_verify_iso_and_publishes_the_certificate() -> None:
    entrypoint = (ROOT / "iso/builder-entrypoint.sh").read_text()
    publish = entrypoint[entrypoint.index("publish_iso() {"):entrypoint.index("\n}\n", entrypoint.index("publish_iso() {"))]
    assert publish.index("iso/sign-secure-boot.py") < publish.index("scripts/verify-iso.py") < publish.index('"${DIST}/.${ISO_NAME}.partial"')
    for needle in ('"${DIST}/${ISO_NAME}.secureboot.cer"', '"${DIST}/${ISO_NAME}.secureboot.txt"', '--secure-boot "${BUILD_DIR}/secure-boot.json"'):
        assert needle in publish


def test_the_builder_image_has_the_tools() -> None:
    containerfile = (ROOT / "iso/Containerfile").read_text()
    for atom in ("app-crypt/sbsigntools", "sys-boot/shim", "sys-fs/dosfstools", "sys-fs/mtools"):
        assert atom in containerfile
    assert "sys-boot/shim -secureboot" in containerfile, "shim stays as Fedora signed it (the ebuild would sign MokManager with a key of ours)"
    # the slow layer (Catalyst) stays cached: the Secure Boot tools are a layer of their own, after it
    assert containerfile.index("dev-util/catalyst") < containerfile.index("app-crypt/sbsigntools")
