"""Wave 6: the AI rescue stick and the SystemRescue-style tools (iso owner)."""
from __future__ import annotations

import json
import re

from .conftest import ROOT, load_script

verify = load_script("scripts/verify-iso.py", "verify_iso")
CONTRACT = json.loads((ROOT / "iso/image-contract.json").read_text())

# The packages of the AI tooling and of the SystemRescue-style set (wave 6, "iso" section).
AI_TOOLING = ["media-gfx/qrencode", "www-apps/ttyd", "sys-apps/ripgrep"]
RESCUE_TOOLS = [
    "app-backup/snapper", "app-backup/fsarchiver", "sys-fs/ncdu", "net-analyzer/tcpdump", "net-analyzer/mtr",
    "sys-fs/dd-rescue", "sys-fs/dislocker", "app-forensics/foremost", "sys-apps/memtester", "app-backup/restic",
    "net-misc/rclone", "net-fs/cifs-utils", "app-editors/vim", "app-misc/mc",
]


def stage1_packages() -> set[str]:
    text = (ROOT / "iso/specs/livecd-stage1.spec").read_text()
    return {re.sub(r"^[=<>~]+", "", a) for a in verify.spec_list(verify.parse_spec(text), "livecd/packages")}


def test_stage1_builds_the_ai_tooling_and_the_rescue_tools() -> None:
    assert set(AI_TOOLING + RESCUE_TOOLS) <= stage1_packages()


def test_the_contract_requires_every_one_of_them_in_the_image() -> None:
    assert set(AI_TOOLING + RESCUE_TOOLS) <= set(CONTRACT["squashfs"]["required_packages"])


def test_ttyd_gets_a_libwebsockets_with_tls_and_libuv() -> None:
    """www-apps/ttyd depends on net-libs/libwebsockets[libuv,ssl]; Catalyst does not auto-unmask USE changes."""
    use = (ROOT / "iso/portage/package.use/live-installer").read_text()
    match = re.search(r"^net-libs/libwebsockets\s+(.*)$", use, re.M)
    assert match and {"libuv", "ssl"} <= set(match.group(1).split())


def test_ttyd_needs_no_keyword_while_the_pinned_tree_has_a_stable_version() -> None:
    """ttyd-1.7.7-r1 is KEYWORDS=amd64 in the tree (the research read the ~amd64 -r2). iso/check-atoms.py fails the
    build on a bump that drops it to ~amd64; the keyword is then added there, not guessed now."""
    keywords = (ROOT / "iso/portage/package.accept_keywords/live-installer").read_text()
    assert "ttyd" not in keywords


def test_the_console_hint_names_the_ai_tool_only_when_the_disc_has_it(tmp_path) -> None:
    import os
    import subprocess

    from .conftest import write_executable

    script = ROOT / "iso/root_overlay/etc/profile.d/10-gentoo-rescue-hint.sh"
    base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    without = subprocess.run(["bash", "--norc", "-i", "-c", f". {script}"], capture_output=True, text=True, env=base, check=False)
    assert "gentoo-rescue-ai" not in without.stdout
    write_executable(tmp_path / "gentoo-rescue-ai", "#!/bin/sh\n")
    withit = subprocess.run(["bash", "--norc", "-i", "-c", f". {script}"], capture_output=True, text=True,
                            env={**base, "PATH": f"{tmp_path}:{base['PATH']}"}, check=False)
    assert "sudo gentoo-rescue-ai" in withit.stdout
    assert max(len(line) for line in withit.stdout.splitlines()) <= 80
