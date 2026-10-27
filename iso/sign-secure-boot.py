#!/usr/bin/env python3
"""Make the finished ISO start with Secure Boot on (the live disc only).

Called by iso/builder-entrypoint.sh (publish_iso) after Catalyst wrote the ISO and before
scripts/verify-iso.py checks it. The first real laptop with Secure Boot on (an ASUS ROG Strix
G15, RC1) refused the disc with "Secure Boot Violation": Catalyst's GRUB is not signed.

The UEFI boot files after this step are in /efi.img, the El Torito UEFI image, which is also
partition 2 of the hybrid GPT that a USB stick boots from:
  /EFI/BOOT/BOOTX64.EFI   Fedora's shim (sys-boot/shim), signed by Microsoft's third-party UEFI CA
  /EFI/BOOT/mmx64.efi     MokManager, signed by Fedora (shim trusts Fedora's key)
  /EFI/BOOT/grubx64.efi   a standalone GRUB, signed with this build's key: with Secure Boot on GRUB
                          loads no modules or fonts from the disc, so every module is built in
  /EFI/BOOT/BOOTIA32.EFI  Catalyst's 32-bit GRUB, unchanged (32-bit UEFI needs Secure Boot off)
  /ENROLL_THIS_KEY_IN_MOKMANAGER.cer   the certificate, for MokManager's "Enroll key from disk"
and /boot/gentoo, the kernel, is signed with the same key (GRUB has shim check it).

On the first start with Secure Boot on, shim cannot verify GRUB and opens MokManager; the user
enrolls the certificate once, and the disc starts. With Secure Boot off nothing changes.

The key is made for this one ISO, in memory (/dev/shm when it exists), and destroyed before
this program ends; only the certificate is kept, next to the ISO and on it. A leaked key can
therefore not sign anything else, and every new ISO needs the one enrollment again.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CERT_NAME = "ENROLL_THIS_KEY_IN_MOKMANAGER.cer"
EFI_LABEL = "GENTOO_EFI"
# Everything grub.cfg (iso/cd_overlay/boot/grub/grub.cfg.in) needs; grub-mkimage adds the dependencies.
GRUB_MODULES = (
    "normal", "configfile", "search", "search_fs_file", "search_label", "iso9660", "fat", "part_gpt", "part_msdos",
    "linux", "boot", "chain", "efifwsetup", "reboot", "halt", "test", "echo", "sleep", "true", "ls", "cat",
    "loadenv", "minicmd", "regexp", "gettext", "terminal", "serial", "all_video", "video", "efi_gop", "efi_uga",
    "video_bochs", "video_cirrus", "font", "gfxterm", "gfxterm_background", "gfxmenu", "png", "jpeg",
)
SHIM_FILES = {"BOOTX64.EFI": "BOOTX64.EFI", "mmx64.efi": "mmx64.efi"}  # /usr/share/shim name -> name on the disc
# What grub-mkrescue (Catalyst) sets up and boot_options() writes again (xorriso -report_el_torito as_mkisofs).
CATALYST_BOOT = ("--grub2-mbr", "--protective-msdos-label", "-efi-boot-part --efi-boot-image", "-b '/boot/grub/i386-pc/eltorito.img'",
                 "--grub2-boot-info", "-e '/efi.img'")


class SignError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tools:
    shim_dir: Path = Path("/usr/share/shim")
    grub_dir: Path = Path("/usr/lib/grub/x86_64-efi")
    sbat: Path = Path("/usr/share/grub/sbat.csv")


def run(*command: str | Path) -> str:
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SignError(f"{Path(str(command[0])).name} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[-600:]}")
    return result.stdout


def early_config(marker: str) -> str:
    """The configuration built into grubx64.efi: find this disc the way Catalyst's GRUB does, then read its menu."""
    if not re.fullmatch(r"/\.disk/[0-9-]+\.uuid", marker):
        raise SignError(f"unexpected marker file {marker!r}")
    return (f"search --no-floppy --set=root --file {marker}\n"
            "set prefix=($root)/boot/grub\n"
            "configfile $prefix/grub.cfg\n")


def efi_image_kib(sizes: list[int]) -> int:
    """A FAT image with room for the files: each rounded up to 4 KiB clusters, plus 1 MiB, in whole MiB."""
    used = sum(-(-size // 4096) * 4 for size in sizes)
    return -(-(used + 1024) // 1024) * 1024


def mokmanager_fingerprint(certificate_der: bytes) -> str:
    """The [Fingerprint] that MokManager's "View key" shows: SHA-1 of the certificate, upper-case byte pairs."""
    digest = hashlib.sha1(certificate_der).hexdigest().upper()
    return " ".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def marker_file(listing: str) -> str:
    """The /.disk/<date>.uuid file that grub-mkrescue's images search for (xorriso -find output)."""
    found = sorted({m.group(1) for m in re.finditer(r"'(/\.disk/[0-9-]+\.uuid)'", listing)})
    if len(found) != 1:
        raise SignError(f"expected one /.disk/*.uuid marker in the ISO, found {found}")
    return found[0]


def volume_date(pvd_info: str) -> str:
    match = re.search(r"^Modif\. Time\s*:\s*(\d{16})\s*$", pvd_info, re.M)
    if not match:
        raise SignError("the ISO has no modification time to keep")
    return match.group(1)


def boot_options(original: Path, date: str) -> list[str]:
    """grub-mkrescue's hybrid boot setup, for the rewritten ISO: GRUB's BIOS MBR code from the original's first 16
    sectors, El Torito for BIOS and UEFI, and GPT partition 2 = /efi.img for USB sticks. "-boot_image any replay" cannot
    do it (libisofs: "GPT partitions 1 and 2 overlap"), and neither can the HFS+ part for old Macs, which is left out:
    Intel Macs start the EFI partition like any other USB stick."""
    return [f"--modification-date={date}", "--grub2-mbr", f"--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt,zero_apm:{original}",
            "--protective-msdos-label", "-partition_cyl_align", "off", "--mbr-force-bootable", "-c", "/boot.catalog",
            "-b", "/boot/grub/i386-pc/eltorito.img", "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table", "--grub2-boot-info",
            "-eltorito-alt-boot", "-e", "/efi.img", "-no-emul-boot", "-efi-boot-part", "--efi-boot-image"]


def make_key(directory: Path, subject: str) -> tuple[Path, Path, Path]:
    key, pem, der = directory / "key.pem", directory / "cert.pem", directory / "cert.der"
    run("openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650", "-subj", subject,
        "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "keyUsage=critical,digitalSignature",
        "-addext", "extendedKeyUsage=codeSigning", "-keyout", key, "-out", pem)
    run("openssl", "x509", "-in", pem, "-outform", "DER", "-out", der)
    return key, pem, der


def destroy(directory: Path) -> None:
    for path in directory.glob("*"):
        if path.is_file():
            subprocess.run(["shred", "-u", str(path)], capture_output=True, check=False)
    shutil.rmtree(directory, ignore_errors=True)


def secret_directory() -> Path:
    shm = Path("/dev/shm")
    return Path(tempfile.mkdtemp(prefix="gentoo-secure-boot.", dir=shm if shm.is_dir() and os.access(shm, os.W_OK) else None))


def sign_iso(iso: Path, work: Path, cert_out: Path, tools: Tools = Tools()) -> dict:
    for name in SHIM_FILES:
        if not (tools.shim_dir / name).is_file():
            raise SignError(f"{tools.shim_dir / name} is missing: the builder image needs sys-boot/shim")
    missing = [m for m in GRUB_MODULES if not (tools.grub_dir / f"{m}.mod").is_file()]
    if missing:
        raise SignError(f"GRUB modules missing in {tools.grub_dir}: {', '.join(missing)}")
    work.mkdir(parents=True, exist_ok=True)
    os.environ["MTOOLS_SKIP_CHECK"] = "1"  # mtools otherwise refuses images whose geometry it does not like
    marker = marker_file(run("xorriso", "-indev", iso, "-find", "/.disk", "-name", "*.uuid"))
    date = volume_date(run("xorriso", "-indev", iso, "-pvd_info"))
    run("xorriso", "-osirrox", "on", "-indev", iso, "-extract", "/efi.img", work / "catalyst-efi.img",
        "-extract", "/boot/gentoo", work / "gentoo")
    run("mcopy", "-n", "-i", work / "catalyst-efi.img", "::/efi/boot/bootia32.efi", work / "BOOTIA32.EFI")
    (work / "early.cfg").write_text(early_config(marker), encoding="ascii")
    run("grub-mkimage", "-O", "x86_64-efi", "-d", tools.grub_dir, "-p", "/boot/grub", "--sbat", tools.sbat,
        "-c", work / "early.cfg", "-o", work / "grubx64.unsigned.efi", *GRUB_MODULES)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Gentoo installer live disc {iso.name} {stamp}"
    secrets = secret_directory()
    try:
        key, pem, der = make_key(secrets, f"/CN={subject}/")
        for unsigned, signed in ((work / "grubx64.unsigned.efi", work / "grubx64.efi"), (work / "gentoo", work / "gentoo.signed")):
            run("sbsign", "--key", key, "--cert", pem, "--output", signed, unsigned)
            run("sbverify", "--cert", pem, signed)
        shutil.copyfile(der, work / CERT_NAME)
        shutil.copyfile(pem, work / "cert.pem")
    finally:
        destroy(secrets)

    files = {"EFI/BOOT/BOOTX64.EFI": tools.shim_dir / "BOOTX64.EFI", "EFI/BOOT/mmx64.efi": tools.shim_dir / "mmx64.efi",
             "EFI/BOOT/grubx64.efi": work / "grubx64.efi", "EFI/BOOT/BOOTIA32.EFI": work / "BOOTIA32.EFI",
             CERT_NAME: work / CERT_NAME}
    image = work / "efi.img"
    image.unlink(missing_ok=True)
    run("mkfs.vfat", "-C", "-n", EFI_LABEL, image, str(efi_image_kib([p.stat().st_size for p in files.values()])))
    run("mmd", "-i", image, "::/EFI", "::/EFI/BOOT")
    for target, source in files.items():
        run("mcopy", "-i", image, source, f"::/{target}")

    rewritten = iso.with_name(iso.name + ".secure-boot")
    rewritten.unlink(missing_ok=True)
    original = run("xorriso", "-indev", iso, "-report_el_torito", "as_mkisofs")
    for needle in CATALYST_BOOT:
        if needle not in original:
            raise SignError(f"Catalyst's ISO no longer has {needle!r} in its boot setup: update iso/sign-secure-boot.py")
    # grub-mkrescue also puts the EFI files into the ISO 9660 tree (/efi/boot) for firmware that reads it: the same chain there
    run("xorriso", "-indev", iso, "-outdev", rewritten,
        "-map", image, "/efi.img", "-map", work / "gentoo.signed", "/boot/gentoo", "-map", work / CERT_NAME, f"/{CERT_NAME}",
        "-map", tools.shim_dir / "BOOTX64.EFI", "/efi/boot/bootx64.efi", "-map", tools.shim_dir / "mmx64.efi", "/efi/boot/mmx64.efi",
        "-map", work / "grubx64.efi", "/efi/boot/grubx64.efi",
        "-boot_image", "any", "discard", "-as", "mkisofs", *boot_options(iso, date), "--")
    report = run("xorriso", "-indev", rewritten, "-report_el_torito", "as_mkisofs", "-report_system_area", "plain")
    for needle in ("-e '/efi.img'", "--efi-boot-image", f"--modification-date='{date}'"):
        if needle not in report:
            raise SignError(f"the rewritten ISO lost {needle!r} from its boot setup")
    if not re.search(r"^GPT partition path :\s+\d+\s+/efi\.img$", report, re.M):
        raise SignError("the rewritten ISO has no GPT partition for /efi.img, so a USB stick would not boot it in UEFI mode")
    os.replace(rewritten, iso)
    shutil.copyfile(work / CERT_NAME, cert_out)
    der_bytes = (work / CERT_NAME).read_bytes()
    return {"certificate": cert_out.name, "certificate_sha256": hashlib.sha256(der_bytes).hexdigest(),
            "mokmanager_fingerprint": mokmanager_fingerprint(der_bytes), "subject": subject, "subject_date": stamp,
            "shim_sha256": hashlib.sha256((tools.shim_dir / "BOOTX64.EFI").read_bytes()).hexdigest(), "marker": marker,
            "grub_modules": list(GRUB_MODULES)}


def notice(iso_name: str, summary: dict) -> str:
    """<iso>.secureboot.txt, published next to the ISO: what MokManager's "View key 0" must show."""
    fingerprint = summary["mokmanager_fingerprint"].split()
    return (f"Secure Boot key of {iso_name}\n\n"
            f"MokManager, \"View key 0\":\n"
            f"  [Subject]      CN={summary['subject']}\n"
            f"  [Fingerprint]  {' '.join(fingerprint[:10])}\n"
            f"                 {' '.join(fingerprint[10:])}\n\n"
            f"SHA-256 of {CERT_NAME}: {summary['certificate_sha256']}\n\n"
            "The key was made for this ISO only and its private half was destroyed when the ISO\n"
            "was built: it can sign nothing else. Enroll it only when the fingerprint matches.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("iso", type=Path, help="the ISO Catalyst wrote; it is replaced by the signed one")
    parser.add_argument("--work", type=Path, required=True, help="an empty directory for the intermediate files")
    parser.add_argument("--cert-out", type=Path, required=True, help="where to keep the certificate (DER)")
    parser.add_argument("--summary", type=Path, help="write what was signed as JSON")
    parser.add_argument("--notice-out", type=Path, help="write the fingerprint text that is published next to the ISO")
    args = parser.parse_args(argv)
    try:
        summary = sign_iso(args.iso, args.work, args.cert_out)
    except SignError as exc:
        print(f"sign-secure-boot: {exc}", file=sys.stderr)
        return 1
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.notice_out:
        args.notice_out.write_text(notice(args.iso.name, summary), encoding="utf-8")
    print(f"sign-secure-boot: {args.iso.name} starts with Secure Boot on; certificate {args.cert_out.name} "
          f"(sha256 {summary['certificate_sha256'][:16]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
