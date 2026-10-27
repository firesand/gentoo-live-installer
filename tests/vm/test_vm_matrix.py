"""scripts/vm_matrix.py and test-vm.py --matrix: the Wave 3 matrix as data, and a scheduler that never runs more than two VMs or more memory than allowed.

Nothing starts a VM: the scheduler gets a fake process spawner, a fake clock and a fake reading of the host's free memory.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import vm_matrix
from tests.vm.helpers import load_vm_module

vm = load_vm_module()

EXPECTED = ["defaults", "rescue-gentoo", "uefi-nvme-luks-openrc", "systemd", "openrc", "uefi-luks", "uefi-luks-lvm", "uefi-manual-two-disks", "bios",
            "bios-sata-minimal", "text-install", "secure-boot-live"]


# --- the data -----------------------------------------------------------------------------------------------------

def test_wave3_has_every_scenario_the_plan_names_and_each_exactly_once():
    wave3 = vm_matrix.MATRICES["wave3"]
    assert [entry.name for entry in wave3.entries] == EXPECTED and len({entry.name for entry in wave3.entries}) == len(wave3.entries)
    wanted = {"defaults", "bios-sata-minimal", "uefi-nvme-luks-openrc", "uefi-manual-two-disks", "openrc", "systemd", "uefi-luks", "uefi-luks-lvm", "bios", "rescue-gentoo",
              "secure-boot-live"}  # RC3: a release must start with Secure Boot on, on every new key
    assert wanted <= {entry.scenario for entry in wave3.entries}
    assert all(entry.scenario in vm.SCENARIO_MAP for entry in wave3.entries)
    assert not [entry.name for entry in wave3.entries if entry.name.startswith("rescue-") and entry.name != "rescue-gentoo"]  # only Gentoo is repaired: no cloud image of another distribution
    assert "distributions" not in wave3.title


def test_every_entry_documents_its_memory_and_cpus_and_the_biggest_job_is_the_desktop():
    for entry in vm_matrix.WAVE3.entries:
        assert entry.memory_mib >= 4096 and entry.cpus >= 2 and entry.minutes > 0 and entry.note, entry.name
    defaults = next(entry for entry in vm_matrix.WAVE3.entries if entry.name == "defaults")
    assert defaults.memory_mib == 12288 and "--build-jobs" in defaults.options and "1" == defaults.options[defaults.options.index("--build-jobs") + 1]
    assert defaults.options[defaults.options.index("--swap-mib") + 1] == "4096"  # the rule for desktop installs in docs/TESTING.md
    assert max(entry.memory_mib for entry in vm_matrix.WAVE3.entries) == defaults.memory_mib
    assert defaults.sizing() == "12 GiB RAM, 4 CPUs" and vm_matrix.MatrixEntry("x", "bios", 6144, 2).sizing() == "6 GiB RAM, 2 CPUs"


def parsed(entry):
    shared = ["--iso", "/tmp/x.iso", "--ssh-host", "127.0.0.1", "--ssh-private-key", "/tmp/key"]
    return vm.build_parser().parse_args(vm_matrix.job_argv(entry, port=40222, shared=shared))


def test_only_the_job_with_the_memory_for_pyside_installs_hwmonux():
    """HWMonux pulls PySide, which has no binary package: it is compiled on every install, for an hour, and a 4 GiB VM with two jobs gets the compiler killed (the first real runs)."""
    installing = list(vm_matrix.WAVE3.entries)
    with_hwmonux = [entry for entry in installing if parsed(entry).companions == "both"]
    assert [entry.name for entry in with_hwmonux] == ["defaults"]
    defaults = with_hwmonux[0]
    args = parsed(defaults)
    assert defaults.memory_mib >= 12288 and args.build_jobs == 1 and args.swap_mib >= 4096  # what the desktop install gets, and a person's guided default
    for entry in installing:
        if entry is not defaults:
            assert parsed(entry).companions == "portagehub", entry.name  # PortageHub is a Rust build that a 4 GiB VM does


def test_the_callers_companions_reach_only_the_job_that_does_not_name_its_own():
    """--companions is passed to every child (its default is 'both'), and the entry's option comes later: the install jobs keep PortageHub only."""
    base = ["--matrix", "wave3", "--iso", "a.iso", "--ssh-host", "127.0.0.1", "--ssh-private-key", "k"]
    for caller, defaults_gets in ((None, "both"), ("none", "none"), ("portagehub", "portagehub")):
        args = vm.build_parser().parse_args(base + (["--companions", caller] if caller else []))
        shared = vm_matrix.passthrough_arguments(args)
        assert ["--companions", caller or "both"] in [shared[i:i + 2] for i in range(len(shared))]
        chosen = {entry.name: vm.build_parser().parse_args(vm_matrix.job_argv(entry, port=40222, shared=[*shared, "--ssh-host", "127.0.0.1"])).companions
                  for entry in vm_matrix.WAVE3.entries}
        assert chosen["defaults"] == defaults_gets
        assert {name: value for name, value in chosen.items() if name != "defaults"} == {name: "portagehub" for name in chosen if name != "defaults"}


def test_the_matrix_stays_within_its_memory_budget_with_two_jobs_at_a_time():
    biggest_pair = sorted((entry.memory_mib for entry in vm_matrix.WAVE3.entries), reverse=True)[:vm_matrix.PARALLEL_MAX]
    assert sum(biggest_pair) <= vm_matrix.DEFAULT_BUDGET_MIB


def test_the_defaults_job_has_the_disk_without_a_serial_number_and_the_gui_installer():
    options = vm_matrix.WAVE3.entries[0].options
    assert "--no-disk-serial" in options and options[options.index("--install-mode") + 1] == "gui"


def test_the_two_disk_and_the_luks_jobs_get_the_extra_checks():
    by_name = {entry.name: entry for entry in vm_matrix.WAVE3.entries}
    assert "--fresh-nvram-check" in by_name["uefi-nvme-luks-openrc"].options and "--fresh-nvram-check" in by_name["openrc"].options
    assert "--fresh-nvram-check" not in by_name["bios"].options and "--fresh-nvram-check" not in by_name["bios-sata-minimal"].options
    assert all("--strict-probes" in entry.options for entry in vm_matrix.WAVE3.entries)


@pytest.mark.parametrize("entry", vm_matrix.WAVE3.entries, ids=lambda entry: entry.name)
def test_every_job_is_a_command_line_the_real_parser_and_the_real_checks_accept(entry):
    shared = ["--iso", "/tmp/x.iso", "--ssh-host", "127.0.0.1", "--ssh-private-key", "/tmp/key"]
    args = vm.build_parser().parse_args(vm_matrix.job_argv(entry, port=40222, shared=shared))
    scenario = vm.apply_overrides(vm.SCENARIO_MAP[entry.scenario], args)
    assert args.scenario == entry.scenario and args.memory == entry.memory_mib and args.cpus == entry.cpus and args.ssh_port == 40222
    assert vm.hardware_problems(scenario, args.install_mode) == [] and vm.rescue_problems(scenario, args) == []


def test_a_job_command_line_has_the_callers_options_first_so_the_entry_decides_the_sizing():
    entry = vm_matrix.MatrixEntry("x", "bios", 4096, 2, ("--strict-probes",))
    argv = vm_matrix.job_argv(entry, port=41000, shared=["--memory", "999", "--iso", "i.iso"])
    assert argv == ["--memory", "999", "--iso", "i.iso", "--scenario", "bios", "--memory", "4096", "--cpus", "2", "--ssh-port", "41000", "--strict-probes"]
    assert vm.build_parser().parse_args([*argv, "--ssh-host", "127.0.0.1"]).memory == 4096


def test_selecting_entries_keeps_the_matrix_order_and_rejects_unknown_names():
    wave3 = vm_matrix.WAVE3
    assert [entry.name for entry in vm_matrix.select_entries(wave3, ["text-install", "bios"])] == ["bios", "text-install"]
    assert len(vm_matrix.select_entries(wave3, [])) == len(wave3.entries)
    with pytest.raises(ValueError, match="no entry nope"):
        vm_matrix.select_entries(wave3, ["bios", "nope"])


def test_the_callers_paths_keys_and_timeouts_are_passed_to_every_child():
    args = vm.build_parser().parse_args(["--matrix", "wave3", "--iso", "a.iso", "--ssh-host", "127.0.0.1", "--ssh-port", "40222", "--ssh-private-key", "k",
                                         "--broken-timeout", "900"])
    shared = vm_matrix.passthrough_arguments(args)
    for pair in (["--iso", "a.iso"], ["--ssh-host", "127.0.0.1"], ["--broken-timeout", "900"], ["--boot-timeout", "900"]):
        assert any(shared[i:i + 2] == pair for i in range(len(shared))), pair
    assert "--ssh-port" not in shared and "--scenario" not in shared and "--memory" not in shared


def test_the_plan_lists_every_job_with_its_sizing_and_the_cap():
    text = vm_matrix.format_plan(vm_matrix.WAVE3, vm_matrix.WAVE3.entries)
    assert "at most 2 VMs at a time" in text and "3 GiB of host memory kept free" in text
    for name in EXPECTED:
        assert name in text
    assert "12 GiB" in text and "minutes one after the other" in text
    assert vm_matrix.format_plan(vm_matrix.WAVE3, vm_matrix.WAVE3.entries, parallel=9).count("at most 2 VMs") == 1


def test_free_memory_is_read_from_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       49152000 kB\nMemFree:         1000 kB\nMemAvailable:   10485760 kB\n")
    assert vm_matrix.host_memory_available_mib(meminfo) == 10240
    assert vm_matrix.host_memory_available_mib(tmp_path / "missing") == 0
    meminfo.write_text("MemAvailable: lots\n")
    assert vm_matrix.host_memory_available_mib(meminfo) == 0


# --- the scheduler --------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    def __init__(self, entry_name: str, ends_at: float, code: int, clock: Clock, log: Path, run_dir: str) -> None:
        self.clock, self.ends_at, self.code, self.returncode = clock, ends_at, code, None
        self.terminated = self.killed = False
        log.write_text(f"RUN {entry_name}: logs in {run_dir}\n")

    def poll(self):
        if self.killed or self.terminated:
            self.returncode = -15 if self.terminated and not self.killed else -9
        elif self.clock() >= self.ends_at:
            self.returncode = self.code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class Lab:
    """A spawner whose jobs last ``minutes`` of fake time and record when and with what they started."""

    def __init__(self, tmp_path: Path, clock: Clock, results: dict[str, int] | None = None) -> None:
        self.tmp, self.clock, self.results = tmp_path, clock, results or {}
        self.started: list[tuple[float, str, list[str]]] = []
        self.live: list[FakeProcess] = []
        self.peak = 0
        self.entries = {entry.name: entry for entry in vm_matrix.WAVE3.entries}

    def __call__(self, argv: list[str], log: Path) -> FakeProcess:
        scenario = argv[argv.index("--scenario") + 1]
        name = log.stem
        process = FakeProcess(name, self.clock() + self.entries[name].minutes * 60, self.results.get(name, 0), self.clock, log, f"/vm/{name}-run")
        self.started.append((self.clock(), name, argv))
        self.live = [item for item in self.live if item.poll() is None] + [process]
        self.peak = max(self.peak, len(self.live))
        assert scenario == self.entries[name].scenario
        return process


def scheduler(tmp_path, clock, lab, entries=None, **options):
    return vm_matrix.Scheduler(entries or vm_matrix.WAVE3.entries, directory=tmp_path, spawn=lab, clock=clock, sleep=clock.sleep, poll=5.0,
                               available_mib=lambda: 40000, **options)


def test_never_more_than_two_vms_run_at_the_same_time_and_every_job_runs_once(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    results = scheduler(tmp_path, clock, lab, budget_mib=100000).run(["--iso", "x.iso"])
    assert lab.peak == 2 and [name for _t, name, _a in lab.started] == EXPECTED  # in matrix order
    assert [result.name for result in results] == EXPECTED and {result.status for result in results} == {"PASS"}
    assert all(result.run_dir == f"/vm/{result.name}-run" for result in results)


def test_a_third_job_waits_for_a_free_place_and_gets_that_places_port(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    entries = [vm_matrix.WAVE3.entries[i] for i in (3, 4, 5)]  # systemd, openrc, uefi-luks
    assert [entry.name for entry in entries] == ["systemd", "openrc", "uefi-luks"]
    scheduler(tmp_path, clock, lab, entries=entries, budget_mib=100000, base_port=41000).run([])
    starts = [(t, name, int(a[a.index("--ssh-port") + 1])) for t, name, a in lab.started]
    assert starts[0][2] != starts[1][2] and {starts[0][2], starts[1][2]} == {41000, 41001}
    assert starts[2][0] >= min(entries[0].minutes, entries[1].minutes) * 60 and starts[2][2] in {41000, 41001}  # when the first of the two is done


def test_the_memory_budget_keeps_the_desktop_away_from_a_second_big_job(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    big = vm_matrix.MatrixEntry("systemd", "systemd", 16384, 2, minutes=10)
    small = vm_matrix.MatrixEntry("openrc", "openrc", 4096, 2, minutes=10)
    lab.entries = {"systemd": big, "openrc": small}
    scheduler(tmp_path, clock, lab, entries=[big, small], budget_mib=16384 + 4096).run([])
    assert lab.peak == 2, "16 GiB + 4 GiB fits in a budget of exactly that"
    clock2 = Clock()
    lab2 = Lab(tmp_path, clock2)
    lab2.entries = {"systemd": big, "openrc": small}
    scheduler(tmp_path, clock2, lab2, entries=[big, small], budget_mib=16384 + 4095).run([])
    assert lab2.peak == 1 and [name for _t, name, _a in lab2.started] == ["systemd", "openrc"] and lab2.started[1][0] >= 600


def test_the_hosts_free_memory_is_respected_with_the_margin(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    free = {"mib": 4096 + vm_matrix.MARGIN_MIB - 1}
    entry = vm_matrix.MatrixEntry("bios", "bios", 4096, 2, minutes=1)
    lab.entries = {"bios": entry}
    runner = vm_matrix.Scheduler([entry], directory=tmp_path, spawn=lab, clock=clock, sleep=clock.sleep, available_mib=lambda: free["mib"], budget_mib=99999)
    [skipped] = runner.run([])
    assert skipped.status == "SKIPPED" and "not enough memory: 4096 MiB for the VM plus 3072 MiB kept free" in skipped.note and "the host has 7167 MiB" in skipped.note
    assert not lab.started
    free["mib"] += 1
    clock2 = Clock()
    lab2 = Lab(tmp_path, clock2)
    lab2.entries = {"bios": entry}
    [ran] = vm_matrix.Scheduler([entry], directory=tmp_path, spawn=lab2, clock=clock2, sleep=clock2.sleep, available_mib=lambda: free["mib"], budget_mib=99999).run([])
    assert ran.status == "PASS" and len(lab2.started) == 1


def test_a_job_that_waits_for_memory_starts_when_another_gives_it_back(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    first = vm_matrix.MatrixEntry("systemd", "systemd", 4096, 2, minutes=5)
    second = vm_matrix.MatrixEntry("openrc", "openrc", 4096, 2, minutes=5)
    lab.entries = {"systemd": first, "openrc": second}
    free = lambda: 4096 + vm_matrix.MARGIN_MIB + (-1 if any(p.poll() is None for p in lab.live) else 5000)  # noqa: E731
    results = vm_matrix.Scheduler([first, second], directory=tmp_path, spawn=lab, clock=clock, sleep=clock.sleep, poll=5.0, available_mib=free, budget_mib=99999).run([])
    assert [r.status for r in results] == ["PASS", "PASS"] and lab.peak == 1 and lab.started[1][0] >= 300


def test_exit_codes_become_verdicts():
    assert [vm_matrix.classify(code) for code in (0, 1, 2, 130, 143, -9, None)] == ["PASS", "FAIL", "USAGE", "KILLED", "KILLED", "FAIL", "FAIL"]


def test_a_failing_job_does_not_stop_the_others(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock, results={"systemd": 1, "openrc": 2})
    entries = [vm_matrix.WAVE3.entries[i] for i in (3, 4, 5)]
    results = scheduler(tmp_path, clock, lab, entries=entries, budget_mib=100000).run([])
    assert [(r.name, r.status, r.exit_code) for r in results] == [("systemd", "FAIL", 1), ("openrc", "USAGE", 2), ("uefi-luks", "PASS", 0)]
    summary = vm_matrix.summarize(vm_matrix.WAVE3, results)
    assert summary["success"] is False and summary["passed"] == ["uefi-luks"] and summary["failed"] == ["systemd", "openrc"]


def test_stopping_asks_every_child_to_clean_up_and_skips_what_never_started(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    runner = scheduler(tmp_path, clock, lab, budget_mib=100000)
    runner.pending = list(vm_matrix.WAVE3.entries[:4])
    runner.entries = list(vm_matrix.WAVE3.entries[:4])
    runner.start(runner.pending.pop(0), [])
    runner.start(runner.pending.pop(0), [])
    runner.stop_all(grace=10)
    assert all(process.terminated for process in lab.live) and not runner.running
    assert [(r.name, r.status) for r in runner.ordered()] == [("defaults", "KILLED"), ("rescue-gentoo", "KILLED"), ("uefi-nvme-luks-openrc", "SKIPPED"), ("systemd", "SKIPPED")]


def test_a_child_that_ignores_the_request_is_killed_after_the_grace_period(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    runner = scheduler(tmp_path, clock, lab, budget_mib=100000)
    runner.entries = [vm_matrix.WAVE3.entries[0]]
    runner.start(vm_matrix.WAVE3.entries[0], [])
    lab.live[0].terminate = lambda: None  # type: ignore[method-assign]
    runner.stop_all(grace=5)
    assert lab.live[0].killed and runner.results[0].status == "KILLED"


def test_the_summary_is_written_next_to_the_logs(tmp_path):
    clock = Clock()
    lab = Lab(tmp_path, clock)
    entries = [vm_matrix.WAVE3.entries[3]]
    results = scheduler(tmp_path, clock, lab, entries=entries, budget_mib=100000).run([])
    path = vm_matrix.write_summary(tmp_path, vm_matrix.WAVE3, results)
    data = json.loads(path.read_text())
    assert path.name == "matrix-result.json" and data["success"] is True and data["jobs"][0]["name"] == "systemd"
    assert data["jobs"][0]["argv"][-len(entries[0].options):] == list(entries[0].options) and data["jobs"][0]["exit_code"] == 0
    assert "--strict-probes" in data["jobs"][0]["argv"] and "--fresh-nvram-check" in data["jobs"][0]["argv"]
    assert "systemd" in vm_matrix.format_results(results) and "PASS" in vm_matrix.format_results(results)


def test_the_run_directory_is_the_last_one_a_child_announced(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("RUN bios: logs in /a\nRUN bios: logs in /b\nPASS bios: result /b/result.json\n")
    assert vm_matrix.read_run_dir(log) == "/b" and vm_matrix.read_run_dir(tmp_path / "missing.log") == ""


def test_a_child_is_a_separate_python_process_with_its_output_in_the_jobs_log(tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import sys\nprint('RUN ' + sys.argv[sys.argv.index('--scenario') + 1] + ': logs in /vm/x', flush=True)\nraise SystemExit(3)\n")
    log = tmp_path / "job.log"
    process = vm_matrix.spawn_child(script)(["--scenario", "bios", "--ssh-port", "40222"], log)
    assert process.wait(timeout=30) == 3
    assert vm_matrix.read_run_dir(log) == "/vm/x" and vm_matrix.classify(process.returncode) == "FAIL"


# --- test-vm.py --matrix -------------------------------------------------------------------------------------------------

def test_listing_the_matrix_does_not_need_an_iso(capsys):
    assert vm.main(["--list-matrix"]) == 0
    out = capsys.readouterr().out
    assert "wave3" in out and "uefi-manual-two-disks" in out and "at most 2 VMs at a time" in out


def test_the_matrix_option_is_exclusive_with_the_other_selections(capsys):
    with pytest.raises(SystemExit) as raised:
        vm.main(["--matrix", "wave3", "--all"])
    assert raised.value.code == 2 and "not allowed with argument" in capsys.readouterr().err


def test_an_unknown_matrix_entry_is_a_usage_error(tmp_path, capsys, monkeypatch):
    from tests.vm.fake_env import Lab as FakeLab

    lab = FakeLab(vm, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as raised:
        vm.main(lab.argv("--matrix", "wave3", "--matrix-only", "bios,nope"))
    assert raised.value.code == 2 and "no entry nope" in capsys.readouterr().err


@pytest.mark.slow
def test_run_matrix_starts_the_children_with_their_own_ports_and_reports_the_verdict(tmp_path, monkeypatch, capsys):
    from tests.vm.fake_env import Lab as FakeLab

    lab = FakeLab(vm, tmp_path, monkeypatch)
    clock = Clock()
    fake = Lab(tmp_path, clock, results={"bios": 1})
    selected = ["bios", "systemd", "openrc"]
    parser = vm.build_parser()
    args = parser.parse_args(lab.argv("--matrix", "wave3", "--matrix-only", ",".join(selected), "--matrix-parallel", "5"))
    args.ssh_port = 42000
    status = vm.run_matrix(args, parser, spawn=fake, available_mib=lambda: 40000, sleep=clock.sleep)
    out = capsys.readouterr().out
    assert status == 1 and fake.peak == 2, "no more than two at once, however many were asked for"
    assert [name for _t, name, _a in fake.started] == ["systemd", "openrc", "bios"]
    ports = {int(a[a.index("--ssh-port") + 1]) for _t, _n, a in fake.started}
    assert ports <= {42000, 42001} and "--iso" in fake.started[0][2] and "--ssh-private-key" in fake.started[0][2]
    assert "START systemd" in out and "FAIL bios" in out and re.search(r"Summary: .*matrix-result\.json", out)
    summaries = list((lab.root).glob("matrix-wave3-*/matrix-result.json"))
    assert len(summaries) == 1 and json.loads(summaries[0].read_text())["failed"] == ["bios"]


@pytest.mark.slow
def test_without_a_port_the_matrix_starts_at_the_default_and_every_slot_is_checked(tmp_path, monkeypatch, capsys):
    from tests.vm.fake_env import Lab as FakeLab

    lab = FakeLab(vm, tmp_path, monkeypatch)
    checked: list[int] = []
    monkeypatch.setattr(vm, "check_port", lambda host, port: checked.append(port))
    clock = Clock()
    fake = Lab(tmp_path, clock)
    parser = vm.build_parser()
    argv = [part for index, part in enumerate(lab.argv("--matrix", "wave3", "--matrix-only", "bios,openrc")) if part not in ("--ssh-port", str(lab.port))]
    args = parser.parse_args(argv)
    assert args.ssh_port is None
    assert vm.run_matrix(args, parser, spawn=fake, available_mib=lambda: 40000, sleep=clock.sleep) == 0
    assert checked == [vm_matrix.DEFAULT_BASE_PORT, vm_matrix.DEFAULT_BASE_PORT + 1]
    assert {int(a[a.index("--ssh-port") + 1]) for _t, _n, a in fake.started} == {vm_matrix.DEFAULT_BASE_PORT, vm_matrix.DEFAULT_BASE_PORT + 1}


@pytest.mark.slow
def test_a_port_the_second_job_would_need_that_is_taken_is_reported_before_anything_starts(tmp_path, monkeypatch, capsys):
    from tests.vm.fake_env import Lab as FakeLab

    lab = FakeLab(vm, tmp_path, monkeypatch)

    def check_port(host, port):
        if port == lab.port + 1:
            raise vm.HarnessError(f"SSH forward endpoint {host}:{port} is unavailable: in use")

    monkeypatch.setattr(vm, "check_port", check_port)
    parser = vm.build_parser()
    args = parser.parse_args(lab.argv("--matrix", "wave3", "--matrix-only", "bios,openrc"))
    with pytest.raises(SystemExit) as raised:
        vm.run_matrix(args, parser, spawn=Lab(tmp_path, Clock()), available_mib=lambda: 40000)
    assert raised.value.code == 2 and f"127.0.0.1:{lab.port + 1} is unavailable" in capsys.readouterr().err
    assert not list(lab.root.glob("matrix-*")), "nothing was started or created"


@pytest.mark.slow
def test_a_matrix_that_needs_a_menu_entry_that_is_not_there_stops_before_the_first_job(tmp_path, monkeypatch, capsys):
    from tests.vm.fake_env import Lab as FakeLab
    from tests.vm.iso_fixture import write_iso

    lab = FakeLab(vm, tmp_path, monkeypatch)
    monkeypatch.setattr(vm, "check_port", lambda host, port: None)
    parser = vm.build_parser()
    started: list = []

    def run(*extra):
        args = parser.parse_args(lab.argv("--matrix", "wave3", *extra))
        with pytest.raises(SystemExit) as raised:
            vm.run_matrix(args, parser, spawn=lambda argv, log: started.append(argv), available_mib=lambda: 40000)
        assert raised.value.code == 2
        return capsys.readouterr().err

    write_iso(lab.iso, {"/boot/grub/grub.cfg": b"menuentry 'Desktop' {\n\tlinux /boot/gentoo cdroot\n}\n", "/boot/gentoo": b"k"})
    message = run("--matrix-only", "text-install")
    assert "the boot menu of" in message and "no boot menu entry matches 'text'" in message
    assert not started and not list(lab.root.glob("matrix-*"))
