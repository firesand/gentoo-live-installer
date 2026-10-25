"""Secure Boot and out-of-tree kernel modules in a VM (hardware backlog H04, the evidence B07 step 2 waits for).

The question: on ``gentoo-kernel-bin`` under firmware Secure Boot, does an unsigned module load (the NVIDIA driver is one), and does a
module signed with the user's own key? The installer keeps refusing the proprietary NVIDIA driver under Secure Boot until a VM has shown
what the kernel really does. This module holds the guest-side experiment (a Python program that builds a dummy module against the
kernel's build tree, tries to load it unsigned and signed, and reports), the parser of its report and the decision it supports.

Nothing here starts a process. ``scripts/test-vm.py`` uploads the test-only signing key, runs the experiment in the installed system
(booted under OVMF's Secure Boot build with a variable store that enrols the test certificate) and records the verdict in
``result.json``. NVIDIA itself, RTD3 and suspend can only be tested on real hardware (``scripts/hardware-checklist.sh``).
"""
from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Mapping

from vm_probes import ANSI_RE, ProbeResult, _sudo_problem

# Where the harness puts the test-only key pair on the live system for the installer (manifest.system.secure_boot_key and _certificate are
# paths on the live system) and the installed system for the module signing.
REMOTE_KEY_DIR = "/run/gentoo-vm-secure-boot"
REMOTE_KEY = f"{REMOTE_KEY_DIR}/db.key"
REMOTE_CERT = f"{REMOTE_KEY_DIR}/db.crt"
KEY_SUBJECT = "/CN=Gentoo installer VM test key (test only, not a secret)"


def key_generation_command(directory: str) -> list[str]:
    """The openssl call that makes the test-only key pair (once; the lead then enrols the certificate in the OVMF variable store)."""
    return ["openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-sha256", "-subj", KEY_SUBJECT,
            "-keyout", f"{directory}/db.key", "-out", f"{directory}/db.crt"]


def enrol_command(directory: str, template: str, output: str) -> list[str]:
    """virt-fw-vars (python-virt-firmware) enrolling the test certificate as PK, KEK and db of a copy of OVMF's variable store.

    ``template`` is OVMF_VARS.fd (or the 4M variant) of the same OVMF build as the secure code image; ``output`` is passed to the harness with
    ``--ovmf-vars-file``. The certificate is the owner of the platform in this store, so no Microsoft key is involved.
    """
    return ["virt-fw-vars", "--input", template, "--output", output, "--set-pk", "00000000-0000-0000-0000-000000000000", f"{directory}/db.crt",
            "--add-kek", "00000000-0000-0000-0000-000000000000", f"{directory}/db.crt", "--add-db", "00000000-0000-0000-0000-000000000000",
            f"{directory}/db.crt", "--secure-boot"]


# Run as root in the installed system. Reads one JSON object from standard input ({"key": base64, "cert": base64}); prints one JSON line.
MODULE_EXPERIMENT_SNIPPET = r"""
import base64, glob, json, os, re, subprocess, sys, tempfile

payload = json.loads(sys.stdin.read() or '{}')
report = {'kernel': os.uname().release}


def read(path, default=''):
    try:
        with open(path, 'rb') as stream:
            return stream.read().decode('latin-1')
    except OSError:
        return default


variables = sorted(glob.glob('/sys/firmware/efi/efivars/SecureBoot-*'))
report['secure_boot'] = bool(variables) and read(variables[0])[-1:] == '\x01'
lockdown = re.search(r'\[(\w+)\]', read('/sys/kernel/security/lockdown'))
report['lockdown'] = lockdown.group(1) if lockdown else ''
report['sig_enforce'] = read('/sys/module/module/parameters/sig_enforce').strip()
report['keyrings'] = sorted({name for name in re.findall(r'(\.builtin_trusted_keys|\.secondary_trusted_keys|\.platform|\.machine)', read('/proc/keys'))})
report['tainted_before'] = int(read('/proc/sys/kernel/tainted', '0').strip() or 0)
build = '/lib/modules/%s/build' % report['kernel']
report['build_tree'] = os.path.isdir(build)
if not report['build_tree']:
    report['build'] = 'no kernel build tree at ' + build
    print(json.dumps(report))
    sys.exit(0)
work = tempfile.mkdtemp(prefix='vm-dummy-')
with open(work + '/gentoo_vm_dummy.c', 'w') as stream:
    stream.write('#include <linux/module.h>\nstatic int __init d_init(void){return 0;}\nstatic void __exit d_exit(void){}\n'
                 'module_init(d_init);\nmodule_exit(d_exit);\nMODULE_LICENSE("GPL");\nMODULE_DESCRIPTION("harness dummy module");\n')
with open(work + '/Makefile', 'w') as stream:
    stream.write('obj-m += gentoo_vm_dummy.o\n')
made = subprocess.run(['make', '-C', build, 'M=' + work, 'modules'], capture_output=True, text=True)
module = work + '/gentoo_vm_dummy.ko'
report['build'] = 'ok' if made.returncode == 0 and os.path.exists(module) else ((made.stderr or made.stdout).strip().splitlines() or ['make failed'])[-1]
if report['build'] != 'ok':
    print(json.dumps(report))
    sys.exit(0)


def attempt(label):
    done = subprocess.run(['insmod', module], capture_output=True, text=True)
    loaded = os.path.isdir('/sys/module/gentoo_vm_dummy')
    if loaded:
        subprocess.run(['rmmod', 'gentoo_vm_dummy'], capture_output=True)
    log = subprocess.run(['dmesg'], capture_output=True, text=True).stdout.strip().splitlines()[-6:]
    report[label] = {'loaded': loaded, 'status': done.returncode, 'message': (done.stderr or done.stdout).strip()[-200:], 'dmesg': log}
    report[label + '_tainted'] = int(read('/proc/sys/kernel/tainted', '0').strip() or 0)


attempt('unsigned')
signer = build + '/scripts/sign-file'
if payload.get('key') and payload.get('cert') and os.path.exists(signer):
    with open(work + '/db.key', 'wb') as stream:
        stream.write(base64.b64decode(payload['key']))
    with open(work + '/db.crt', 'wb') as stream:
        stream.write(base64.b64decode(payload['cert']))
    signed = subprocess.run([signer, 'sha512', work + '/db.key', work + '/db.crt', module], capture_output=True, text=True)
    if signed.returncode == 0:
        attempt('signed')
    else:
        report['signed'] = {'loaded': None, 'status': signed.returncode, 'message': 'sign-file failed: ' + signed.stderr.strip()[-200:], 'dmesg': []}
else:
    report['signed'] = {'loaded': None, 'status': None, 'message': 'no test key was given or the kernel has no scripts/sign-file', 'dmesg': []}
print(json.dumps(report))
"""


def module_experiment_command() -> str:
    """Run as root through sudo; standard input is the sudo password line, then the JSON payload."""
    return "sudo -S -p '' " + shlex.join(("python3", "-c", MODULE_EXPERIMENT_SNIPPET))


def module_experiment_input(password: str, key: bytes, cert: bytes) -> bytes:
    payload = {"key": base64.b64encode(key).decode(), "cert": base64.b64encode(cert).decode()}
    return (password + "\n" + json.dumps(payload) + "\n").encode()


def report_of(output: str) -> dict[str, Any] | None:
    for line in reversed(ANSI_RE.sub("", output).strip().splitlines()):
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "kernel" in report:
            return report
    return None


def decision(report: Mapping[str, Any]) -> dict[str, Any]:
    """What this run shows for B07 step 2 (allow the vendor and NVIDIA modules on gentoo-kernel-bin with a warning, or keep refusing)."""
    unsigned = report.get("unsigned") or {}
    signed = report.get("signed") or {}
    facts = {
        "secure_boot": bool(report.get("secure_boot")), "lockdown": report.get("lockdown", ""), "sig_enforce": report.get("sig_enforce", ""),
        "unsigned_module_loads": unsigned.get("loaded"), "module_signed_with_the_test_key_loads": signed.get("loaded"),
        "keyrings": report.get("keyrings", []), "kernel": report.get("kernel", ""),
        "tainted_after_unsigned_load": report.get("unsigned_tainted"),
    }
    if report.get("build") != "ok":
        return {**facts, "verdict": "inconclusive", "why": f"the dummy module could not be built: {report.get('build', 'no information')}"}
    if not facts["secure_boot"]:
        return {**facts, "verdict": "inconclusive",
                "why": "Secure Boot was not active in this run (the variable store must enrol the test certificate, see docs/TESTING.md)"}
    if facts["unsigned_module_loads"]:
        return {**facts, "verdict": "unsigned-modules-load",
                "why": "under Secure Boot this kernel loaded an unsigned module (it taints the kernel): NVIDIA and vendor modules can be allowed "
                       "with a warning on gentoo-kernel-bin; keep refusing when lockdown is on or signatures are enforced"
                       + (f" (lockdown is {facts['lockdown']!r})" if facts["lockdown"] not in ("", "none") else "")}
    detail = "the kernel refused an unsigned module"
    if facts["lockdown"] not in ("", "none"):
        detail += f" (lockdown {facts['lockdown']})"
    if facts["sig_enforce"] in ("Y", "1"):
        detail += " (module.sig_enforce is on)"
    if facts["module_signed_with_the_test_key_loads"]:
        return {**facts, "verdict": "only-signed-modules-load", "why": f"{detail}, and a module signed with the test key loaded: keep refusing in Guided "
                                                                       "mode, a signed path (Advanced, step 3) can work"}
    return {**facts, "verdict": "unsigned-modules-refused",
            "why": f"{detail}, and a module signed with the test key did not load either (the key is not in a keyring the kernel trusts): keep refusing"}


def parse_module_experiment_probe(returncode: int, output: str) -> ProbeResult:
    """The experiment must run and Secure Boot must have been active; the verdict is kept in ``data`` (and printed by the harness)."""
    problem = _sudo_problem("secure-boot-modules", returncode, output)
    if problem:
        return problem
    report = report_of(output)
    if report is None:
        return ProbeResult("secure-boot-modules", False, ANSI_RE.sub("", output).strip()[-400:], f"the experiment returned no report (exit {returncode})")
    verdict = decision(report)
    summary = f"{verdict['verdict']}: {verdict['why']}"
    return ProbeResult("secure-boot-modules", verdict["verdict"] != "inconclusive", summary, "" if verdict["verdict"] != "inconclusive" else verdict["why"],
                       data={"report": report, "decision": verdict})
