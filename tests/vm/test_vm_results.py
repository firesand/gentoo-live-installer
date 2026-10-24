import json
from pathlib import Path

import pytest

from tests.vm.helpers import PUBLIC_KEY, load_vm_module


vm = load_vm_module()


def completed(success):
    return json.dumps({"type": "completed", "data": {"success": success}, "message": "done"})


def test_install_result_requires_zero_exit_and_success_event():
    output = "\n".join((json.dumps({"type": "stage", "stage": "storage"}), completed(True)))
    result = vm.parse_install_result(0, output)
    assert result.success is True
    assert result.error == ""
    assert len(result.events) == 2
    assert vm.parse_install_result(1, output).success is False
    assert vm.parse_install_result(0, completed(False)).success is False
    assert vm.parse_install_result(0, "ordinary output").success is False


def test_install_result_reports_backend_errors_and_ignores_non_json():
    output = "installer banner\n" + json.dumps({"type": "error", "error": "disk failed"}) + "\n" + completed(False)
    result = vm.parse_install_result(1, output)
    assert result.success is False
    assert "disk failed" in result.error
    assert "status 1" in result.error


def test_gui_backend_failure_keeps_the_underlying_error_diagnostic():
    output = "\n".join((
        json.dumps({"type": "error", "error": "installkernel requires USE=dracut"}),
        completed(False),
        json.dumps({"type": "gui_result", "success": False, "reboot_enabled": False}),
    ))
    result = vm.parse_install_result(1, output)
    with pytest.raises(vm.HarnessError, match="Installation failed:.*USE=dracut"):
        vm.validate_install_outcome(result, Path("install.log"), gui=True)


def test_successful_gui_install_still_requires_reboot_ready_indicator():
    result = vm.parse_install_result(0, "\n".join((
        completed(True),
        json.dumps({"type": "gui_result", "success": False, "reboot_enabled": False}),
    )))
    with pytest.raises(vm.HarnessError, match="GUI did not report one successful ready state"):
        vm.validate_install_outcome(result, Path("install.log"), gui=True)


def test_exact_probe_parsing_strips_ansi():
    assert vm.parse_probe_result("hostname", 0, "\x1b[0mgentoo-vm-openrc\x1b[0m\n", "gentoo-vm-openrc").success
    mismatch = vm.parse_probe_result("hostname", 0, "other\n", "gentoo-vm-openrc")
    assert mismatch.success is False
    assert "Expected" in mismatch.error
    assert vm.parse_probe_result("user", 255, "connection closed").success is False


def test_network_result_needs_address_and_default_route():
    output = "2: eth0 inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0\ndefault via 10.0.2.2 dev eth0\n"
    assert vm.parse_network_result(0, output).success is True
    assert vm.parse_network_result(0, "2: eth0 inet 10.0.2.15/24 scope global eth0").success is False
    assert vm.parse_network_result(0, "default via 10.0.2.2 dev eth0").success is False


def test_portage_query_must_return_portage_match():
    output = "[ Results for search key : portage ]\n* sys-apps/portage-3.0.65\n"
    assert vm.ScenarioRunner.parse_portage(0, output).success is True
    result = vm.ScenarioRunner.parse_portage(0, "No matches found")
    assert result.success is False
    assert "no sys-apps/portage" in result.error


def test_gui_environment_requires_a_reported_local_display_and_authority():
    output = 'desktop probe\n' + json.dumps({
        "type": "gui_environment",
        "environment": {"DISPLAY": ":0.0", "XAUTHORITY": "/run/user/1000/Xauthority",
                         "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"},
    })
    env = vm.parse_gui_environment(0, output)
    assert env["DISPLAY"] == ":0.0"
    assert env["XAUTHORITY"] == "/run/user/1000/Xauthority"
    assert vm.parse_gui_environment(0, json.dumps({"type": "gui_environment", "environment": {
        "DISPLAY": "unix:1", "XAUTHORITY": "/tmp/xauth"}}))["DISPLAY"] == "unix:1"


def test_gui_environment_rejects_failed_or_unsafe_probe():
    import pytest

    with pytest.raises(vm.HarnessError, match="inspect the live desktop"):
        vm.parse_gui_environment(1, "probe failed")
    for environment in (
        {"DISPLAY": "tcp:host:0", "XAUTHORITY": "/tmp/xauth"},
        {"DISPLAY": ":0", "XAUTHORITY": "relative"},
        {"XAUTHORITY": "/tmp/xauth"},
    ):
        output = json.dumps({"type": "gui_environment", "environment": environment})
        with pytest.raises(vm.HarnessError):
            vm.parse_gui_environment(0, output)


def test_gui_artifact_decoder_rejects_empty_and_malformed_images():
    import base64
    import pytest

    with pytest.raises(vm.HarnessError, match="not valid base64"):
        vm.decode_gui_artifact("gui.log", "not base64!")
    with pytest.raises(vm.HarnessError, match="empty"):
        vm.decode_gui_artifact("gui.log", "")
    with pytest.raises(vm.HarnessError, match="not a PNG"):
        vm.decode_gui_artifact("review.png", base64.b64encode(b"text").decode())
    assert vm.decode_gui_artifact("gui.log", base64.b64encode(b"ready\n").decode()) == b"ready\n"


def test_hwmonux_runtime_probe_requires_a_report_header():
    assert vm.parse_hwmonux_dump(0, "vm-host\nValue Min Max\n").success
    failed = vm.parse_hwmonux_dump(0, "vm-host\nempty report\n")
    assert not failed.success
    assert "report header" in failed.error
    assert not vm.parse_hwmonux_dump(1, "missing module").success


def test_portagehub_self_test_requires_runtime_sections():
    assert vm.parse_portagehub_self_test(0, "repos: 2\ncatalog: 10\nvdb: 3\n").success
    failed = vm.parse_portagehub_self_test(0, "PortageHub started\n")
    assert not failed.success
    assert "repos, catalog" in failed.error


def test_disabled_companion_probe_requires_package_absence():
    assert vm.parse_companion_absent("hwmonux", 0, "absent\n").success
    result = vm.parse_companion_absent("hwmonux", 1, "present\n")
    assert not result.success
    assert result.name == "disabled-hwmonux-absent"


def test_static_network_probe_requires_active_matching_nm_profile():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["bios"], PUBLIC_KEY)
    probe = vm.static_network_probe(manifest)
    assert probe is not None
    command, parser = probe
    assert "nmcli" in command
    expected = json.dumps({
        "active": ["Installer wired static:enp0s1"],
        "method": "manual",
        "addresses": ["10.0.2.15/24"],
        "gateway": "10.0.2.2",
        "dns": ["10.0.2.3"],
    })
    assert parser(0, expected).success
    inactive = json.loads(expected)
    inactive["active"] = ["Wired connection 1:enp0s1"]
    failed = parser(0, json.dumps(inactive))
    assert not failed.success
    assert "not an active" in failed.error
    extra = json.loads(expected)
    extra["addresses"].append("192.0.2.5/24")
    assert not parser(0, json.dumps(extra)).success
    extra_dns = json.loads(expected)
    extra_dns["dns"].append("192.0.2.53")
    assert not parser(0, json.dumps(extra_dns)).success
    assert not parser(0, '[]').success
    assert not parser(0, json.dumps({"active": "wrong-type"})).success


def test_static_network_probe_is_not_scheduled_for_dhcp_manifest():
    manifest = vm.build_manifest(vm.SCENARIO_MAP["openrc"], PUBLIC_KEY)
    assert vm.static_network_probe(manifest) is None


def _storage_snapshot(order=("/dev/vda1", "/dev/vda2")):
    rows = {
        "/dev/vda": {"type": "disk", "uuid": "", "fstype": "", "partuuid": "",
                     "parttype": "", "pttype": "gpt", "ptuuid": "table-id"},
        "/dev/vda1": {"type": "part", "uuid": "efi-uuid", "fstype": "vfat", "partuuid": "part-1",
                       "parttype": "efi", "pttype": "", "ptuuid": ""},
        "/dev/vda2": {"type": "part", "uuid": "root-uuid", "fstype": "ext4", "partuuid": "part-2",
                       "parttype": "linux", "pttype": "", "ptuuid": ""},
    }
    return {"blockdevices": [{"path": "/dev/vda", **rows["/dev/vda"], "children": [
        {"path": path, **rows[path]} for path in order
    ]}]}


def test_lsblk_snapshot_flattens_and_requires_nonempty_partition_identity():
    first = vm.parse_lsblk_snapshot(json.dumps(_storage_snapshot()))
    second = vm.parse_lsblk_snapshot(json.dumps(_storage_snapshot(("/dev/vda2", "/dev/vda1"))))
    assert first == second
    assert set(first) == {"/dev/vda", "/dev/vda1", "/dev/vda2"}
    broken = _storage_snapshot()
    broken["blockdevices"][0]["children"][0]["uuid"] = None
    import pytest
    with pytest.raises(vm.HarnessError, match="empty filesystem UUID"):
        vm.parse_lsblk_snapshot(json.dumps(broken))


def test_resume_regression_requires_storage_skip_success_and_identical_ids():
    snapshot = vm.parse_lsblk_snapshot(json.dumps(_storage_snapshot()))
    output = "\n".join((
        json.dumps({"type": "stage", "stage": "storage", "data": {"status": "skipped"}}),
        completed(True),
    ))
    report = vm.parse_resume_regression(0, output, snapshot, snapshot)
    assert report["success"] is True
    assert report["storage_skipped"] is True
    changed = dict(snapshot)
    changed["/dev/vda2"] = {**snapshot["/dev/vda2"], "uuid": "new-uuid"}
    assert vm.parse_resume_regression(0, output, snapshot, changed)["success"] is False
    no_skip = vm.parse_resume_regression(0, completed(True), snapshot, snapshot)
    assert no_skip["success"] is False


def test_resume_regression_detects_only_logged_runner_destructive_commands():
    snapshot = vm.parse_lsblk_snapshot(json.dumps(_storage_snapshot()))
    success = json.dumps({"type": "stage", "stage": "storage", "data": {"status": "skipped"}}) + "\n" + completed(True)
    ordinary_text = success + "\npackage output mentions mkfs.ext4"
    assert vm.parse_resume_regression(0, ordinary_text, snapshot, snapshot)["success"] is True
    logged = json.dumps({"type": "log", "message": "$ mkfs.ext4 -F /dev/vda2"})
    report = vm.parse_resume_regression(0, success + "\n" + logged, snapshot, snapshot)
    assert report["success"] is False
    assert report["destructive_commands"] == ["mkfs.ext4 -F /dev/vda2"]


def test_cancel_regression_requires_storage_then_single_stage3_and_false_completion():
    output = "\n".join((
        '{"type":"stage","stage":"storage","data":{"status":"completed"}}',
        '{"type":"stage","stage":"stage3","data":{"status":"running"}}',
        '{"type":"error","error":"cancelled","message":"Installation cancelled"}',
        '{"type":"stage","stage":"cleanup","data":{"status":"completed"}}',
        '{"type":"completed","data":{"success":false}}',
        '{"type":"gui_result","success":false,"cancel_requested":true,"reboot_enabled":false,"stage3_started":1}',
    ))
    report = vm.parse_cancel_regression(0, output)
    assert report["success"] is True

    assert vm.parse_cancel_regression(0, output.replace('"success":false', '"success":true', 1))["success"] is False
    assert vm.parse_cancel_regression(0, output.replace('"error":"cancelled"', '"error":"other"'))["success"] is False
    assert vm.parse_cancel_regression(0, output.replace(
        '{"type":"error","error":"cancelled","message":"Installation cancelled"}',
        '{"type":"error","error":"cancelled","message":"Installation cancelled"}\n{"type":"error","error":"cleanup failed"}',
    ))["success"] is False
    assert vm.parse_cancel_regression(0, output + '\n{"type":"stage","stage":"stage3","data":{"status":"running"}}')["success"] is False


def test_cancel_cleanup_rejects_remaining_mounts_root_stack_or_swap():
    clean = json.dumps({"target_mounts": [], "leftover_root_stack": [], "target_swaps": []})
    assert vm.parse_cancel_cleanup(0, clean)["success"] is True
    for field, value in (("target_mounts", ["/mnt/gentoo"]),
                         ("leftover_root_stack", ["/dev/mapper/gentoo-root"]),
                         ("target_swaps", ["/dev/vda3"])):
        payload = {"target_mounts": [], "leftover_root_stack": [], "target_swaps": []}
        payload[field] = value
        assert vm.parse_cancel_cleanup(0, json.dumps(payload))["success"] is False
