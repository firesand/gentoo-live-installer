"""MokManager on the serial console: enroll the live disc's key, as a person does on the first start with Secure Boot on.

iso/sign-secure-boot.py signs GRUB and the kernel with a key made for one ISO. Firmware with Microsoft's keys runs the disc's shim,
shim cannot verify GRUB and opens MokManager, and the person enrolls ENROLL_THIS_KEY_IN_MOKMANAGER.cer from the GENTOO_EFI volume
(iso/cd_overlay/SECURE-BOOT.txt says the same steps in words). OVMF copies MokManager's screens to the serial console, so each step
waits for its text there and presses the keys of the choice. The screens were read from a real run (scripts/test-vm.py
--scenario secure-boot-live on the RC2 test ISO, 2026-10-27).
"""
from __future__ import annotations

import re
import time
from typing import Callable, Sequence

CERT_NAME = "ENROLL_THIS_KEY_IN_MOKMANAGER.cer"   # iso/sign-secure-boot.py CERT_NAME: on the ISO root and on GENTOO_EFI
# (what the screen shows: a regular expression for the serial console, the keys that answer it)
ENROLL_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"Verification failed: \(0x1A\) Security Violation", ("ret",)),          # shim: GRUB's key is not enrolled yet -> OK
    (r"Press any key to perform MOK management", ("down",)),                  # 10 seconds, then shim gives up
    (r"(?s)Perform MOK management.*?Enroll key from disk", ("down", "ret")),  # Continue boot / Enroll key from disk / Enroll hash
    (r"(?s)Select Key.*?GENTOO_EFI", ("ret",)),                               # the volume list: the disc's EFI image only
    (r"ENROLL_THIS_KEY_IN_MOKMANAGER\.cer", ("down", "ret")),                 # EFI/ first, then the certificate
    (r"(?s)Enroll MOK\].*?View key 0.*?Continue", ("down", "ret")),           # View key 0 / Continue
    (r"Enroll the key\(s\)\?", ("down", "ret")),                              # No / Yes
    (r"(?s)Perform MOK management.*?Reboot", ("ret",)),                       # Reboot (QEMU runs with -no-reboot: it exits)
)
SETTLE_SECONDS = 0.8   # MokManager draws the whole screen before it reads keys
KEY_SECONDS = 0.4


def enroll(wait_for: Callable[[str, int, int], int], press: Callable[[str], None], *, timeout: int = 120,
           steps: Sequence[tuple[str, tuple[str, ...]]] = ENROLL_STEPS, sleep: Callable[[float], None] = time.sleep,
           on_step: Callable[[int, str], None] | None = None) -> list[str]:
    """Walk through MokManager. ``wait_for(pattern, timeout, since)`` returns the offset after the match (SerialChannel.wait_for),
    ``press(qcode)`` presses one key. Returns the patterns in the order they were answered."""
    since, answered = 0, []
    for index, (pattern, keys) in enumerate(steps):
        since = wait_for(pattern, timeout, since)
        sleep(SETTLE_SECONDS)
        if on_step is not None:
            on_step(index, pattern)
        for key in keys:
            press(key)
            sleep(KEY_SECONDS)
        answered.append(pattern)
    return answered


# What the live system says after the second start (scripts/test-vm.py, SECURE_BOOT_PROBE).
SECURE_BOOT_PROBE = ("mokutil --sb-state; echo '--- enrolled'; mokutil --list-enrolled | grep -E 'Subject:|Issuer:' ; "
                     "echo '--- export'; d=$(mktemp -d) && cd \"$d\" && mokutil --export >/dev/null 2>&1; sha256sum MOK-*.der 2>/dev/null; "
                     "echo '--- dmesg'; dmesg | grep -i 'secure boot' ; true")


def secure_boot_problems(output: str, certificate_sha256: str) -> list[str]:
    """Secure Boot must be on, and the disc's own certificate (and nothing that only looks like it) must be in the MOK list."""
    problems = []
    if not re.search(r"^SecureBoot enabled$", output, re.M):
        problems.append("mokutil --sb-state does not say 'SecureBoot enabled'")
    if "CN=Gentoo installer live disc" not in output.split("--- export")[0]:
        problems.append("the MOK list has no key named 'Gentoo installer live disc'")
    exported = re.findall(r"^([0-9a-f]{64})\s+\S*MOK-\d+\.der$", output, re.M)
    if certificate_sha256 not in exported:
        problems.append(f"no enrolled key is the disc's certificate (sha256 {certificate_sha256[:16]}...; enrolled: "
                        f"{', '.join(value[:16] for value in exported) or 'none'})")
    if not re.search(r"(?i)secure ?boot enabled", output.split("--- dmesg")[-1]):
        problems.append("the kernel did not see Secure Boot (dmesg)")
    return problems
