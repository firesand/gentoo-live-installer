"""Preflight: the dry-run preview (S8), the mirror/proxy probe and severity-based fatality (S11), storage stage logging."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gentoo_installerd import inspect as insp
from gentoo_installerd.inspect import Discovery, Inspection
from gentoo_installerd.install import InstallError, Installer
from gentoo_installerd.runner import DryRunRunner
from gentoo_installerd.server import main

from tests.backend.helpers import FakeInspector, FakeRunner, lsblk, manifest

GiB = 2**30


class StaticInspector:
    def __init__(self, inspection: Inspection) -> None:
        self.inspection = inspection

    def inspect(self) -> Inspection:
        return self.inspection


def inspection(*, errors=(), warnings=(), findings=(), tools=None, uefi=True) -> Inspection:
    return Inspection("x86_64", 8 * GiB, uefi, True, tools if tools is not None else {"lsblk": True}, [], list(errors), list(warnings), list(findings))


def installer(tmp_path: Path, inspector, *, dry_run: bool = False, **changes) -> Installer:
    m = manifest()
    for name, value in changes.items():
        section, _, field = name.partition("__")
        setattr(getattr(m, section), field, value)
    return Installer(m, runner=FakeRunner(), target_root=tmp_path / "target", state_dir=tmp_path / "state", cache_dir=tmp_path / "cache", dry_run=dry_run, inspector=inspector)


class TestDryRunPreview:
    def run_cli(self, tmp_path: Path, monkeypatch) -> list[dict]:
        monkeypatch.setattr(insp, "_network_ready", lambda *a, **k: False)
        path = tmp_path / "manifest.json"
        m = manifest()
        m.portage.mirror = "https://mirror.example.org:8443/gentoo"
        m.network.proxy = "http://proxy.lan:3128"
        path.write_text(m.to_json())
        output = io.StringIO()
        with patch("os.geteuid", return_value=1000), contextlib.redirect_stdout(output):
            code = main(["--manifest", str(path), "--dry-run", "--target-root", str(tmp_path / "target"), "--state-dir", str(tmp_path / "state"), "--cache-dir", str(tmp_path / "cache")])
        assert code == 0, output.getvalue()[-2000:]
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_preview_succeeds_with_the_real_inspector_and_shows_the_plan(self, tmp_path, monkeypatch):
        events = self.run_cli(tmp_path, monkeypatch)
        logs = [event["message"] for event in events if event["type"] == "log"]
        assert events[-1]["data"]["success"] is True
        assert any("[dry-run] sgdisk --zap-all /dev/sda" in line for line in logs)
        assert any(line.startswith("Storage plan: ERASE  /dev/sda") for line in logs)

    def test_findings_that_would_block_a_real_run_are_reported_not_fatal(self, tmp_path, monkeypatch):
        logs = [event["message"] for event in self.run_cli(tmp_path, monkeypatch) if event["type"] == "log"]
        finding = next(line for line in logs if line.startswith("[dry-run] preflight finding (fatal in a real run)"))
        assert "No network connection to the stage3 mirror mirror.example.org:8443 through the proxy proxy.lan:3128" in finding

    def test_the_preview_reads_the_disks_through_a_read_only_runner(self, tmp_path):
        discovery = Discovery(DryRunRunner())
        assert isinstance(discovery.reader, insp.ReadOnlyRunner)


class TestRealRunPreflight:
    def test_an_unreachable_mirror_stops_a_real_run_and_names_host_and_proxy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(insp, "_network_ready", lambda *a, **k: False)
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        inst = installer(tmp_path, Discovery(runner), portage__mirror="https://mirror.example.org:8443", network__proxy="http://proxy.lan:3128")
        with pytest.raises(InstallError, match="Preflight failed: No network connection to the stage3 mirror mirror.example.org:8443 through the proxy proxy.lan:3128"):
            inst._preflight()

    def test_the_probe_is_pointed_at_the_manifests_mirror_and_proxy(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(insp, "_network_ready", lambda host, port, proxy="", timeout=5.0: calls.append((host, port, proxy)) or True)
        runner = FakeRunner()
        runner.respond("lsblk", lsblk())
        discovery = Discovery(runner)
        inst = installer(tmp_path, discovery, portage__mirror="https://mirror.example.org", network__proxy="http://proxy.lan:3128")
        with contextlib.suppress(InstallError):  # this machine's firmware mode and tools are irrelevant here
            inst._preflight()
        assert calls and calls[0] == ("mirror.example.org", 443, "http://proxy.lan:3128")

    def test_memory_advice_is_a_structured_warning_not_a_message_prefix(self, tmp_path):
        logs: list[str] = []
        warning = {"code": "preflight.memory_low", "severity": "warning", "message": "At least 2 GiB of memory is recommended"}
        inst = installer(tmp_path, StaticInspector(inspection(warnings=[warning["message"]], findings=[warning])))
        inst.log = logs.append  # type: ignore[method-assign]
        inst._preflight()
        assert "Preflight warning: At least 2 GiB of memory is recommended" in logs

    def test_only_severity_error_findings_block(self, tmp_path):
        findings = [
            {"code": "preflight.memory_low", "severity": "warning", "message": "At least 2 GiB of memory is recommended"},
            {"code": "preflight.network", "severity": "error", "message": "No network connection to the stage3 mirror example.org:443 was detected"},
        ]
        inst = installer(tmp_path, StaticInspector(inspection(errors=[findings[1]["message"]], warnings=[findings[0]["message"]], findings=findings)))
        with pytest.raises(InstallError) as caught:
            inst._preflight()
        assert str(caught.value) == "Preflight failed: No network connection to the stage3 mirror example.org:443 was detected"

    def test_legacy_inspectors_that_only_fill_errors_still_block(self, tmp_path):
        with pytest.raises(InstallError, match="Preflight failed: disk is on fire"):
            installer(tmp_path, FakeInspector(["disk is on fire"]))._preflight()

    def test_a_uefi_install_from_a_bios_boot_says_how_to_fix_it(self, tmp_path):
        with pytest.raises(InstallError, match="started in legacy BIOS \\(CSM\\) mode.*Legacy BIOS in the storage settings"):
            installer(tmp_path, StaticInspector(inspection(uefi=False)))._preflight()

    def test_btrfs_installs_need_the_btrfs_tool(self, tmp_path):
        inst = installer(tmp_path, StaticInspector(inspection(tools={"btrfs": False, "mkfs.btrfs": True, "mkfs.vfat": True})), storage__filesystem="btrfs")
        with pytest.raises(InstallError, match="needs missing tools: btrfs"):
            inst._preflight()

    def test_missing_tools_are_only_logged_in_a_preview(self, tmp_path):
        logs: list[str] = []
        inst = installer(tmp_path, StaticInspector(inspection(tools={"mkfs.vfat": False})), dry_run=True)
        inst.log = logs.append  # type: ignore[method-assign]
        inst._preflight()
        assert any("[dry-run] selected configuration needs tools missing on this machine: mkfs.vfat" in line for line in logs)
