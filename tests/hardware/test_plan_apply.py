"""B06: the schema 2 fields of the hardware plan are applied on the target (groups, keywords, unmask, user services, repos, first steps)."""

from __future__ import annotations

import pytest

from gentoo_installerd import hwplan
from gentoo_installerd.configure_base import ConfigurationError
from gentoo_installerd.configure_hardware import repo_specs
from gentoo_installerd.hwplan import HardwarePlan
from gentoo_installerd.runner import RunnerError

from tests.backend.target_helpers import ScriptedRunner, configurer, read, target_manifest

EDORP = "edorp git https://github.com/firesand/edorp-overlay.git"
GURU = "guru git https://github.com/gentoo/guru.git"


def full_plan(**more) -> HardwarePlan:
    fields = dict(
        groups=["plugdev", "i2c"],
        package_keywords={"=games-util/game-device-udev-rules-20260625": ["~amd64"]},
        package_unmask=["=sys-power/asusctl-6.5.0"],
        user_services=[("openrazer-daemon.service", "service")],
        repos=[GURU, EDORP],
        first_steps=["Open Polychromatic to set the colours of your Razer devices."],
        scalars={"first_user": "ada", "init": "systemd"},
    )
    fields.update(more)
    return HardwarePlan(**fields)


@pytest.fixture
def target(tmp_path):
    runner = ScriptedRunner()
    return configurer(tmp_path, runner), runner, tmp_path


def test_keywords_unmask_and_repos_are_written_for_portage(target):
    c, _runner, root = target
    c.write_plan_files(full_plan())
    assert read(root, "etc/portage/package.accept_keywords/installer-hardware").splitlines()[-1] == "=games-util/game-device-udev-rules-20260625 ~amd64"
    assert "=sys-power/asusctl-6.5.0" in read(root, "etc/portage/package.unmask/installer-hardware")
    edorp = read(root, "etc/portage/repos.conf/edorp.conf")
    assert edorp.splitlines()[0] == "[edorp]" and "sync-type = git" in edorp and "sync-uri = https://github.com/firesand/edorp-overlay.git" in edorp
    assert "location = /var/db/repos/edorp" in edorp
    assert "sync-uri = https://github.com/gentoo/guru.git" in read(root, "etc/portage/repos.conf/guru.conf")


def test_an_empty_plan_writes_none_of_them(target):
    c, _runner, root = target
    c.write_plan_files(HardwarePlan())
    assert not (root / "etc/portage").exists()


@pytest.mark.parametrize("entry", ["edorp", "edorp git http://example.org/x.git", "edorp rsync https://example.org/x", "../x git https://example.org/x.git",
                                   "edorp git https://example.org/x y", "edorp git ssh://git@example.org/x.git"])
def test_a_repository_entry_that_is_not_plain_https_git_is_refused(entry):
    with pytest.raises(ConfigurationError):
        repo_specs(HardwarePlan(repos=[entry]))


def test_group_membership_only_when_the_group_exists(target):
    c, runner, _root = target
    runner.script("chroot", "ok")  # placeholder so the order below is explicit
    runner.commands.clear()
    runner.script("*getent group i2c", ("fail", "no such group", 2))
    warnings = c.join_plan_groups(full_plan())
    lines = runner.lines()
    assert warnings == []
    assert any(line.endswith("usermod -aG plugdev ada") for line in lines)
    assert not any("usermod -aG i2c" in line for line in lines)


def test_user_services_use_the_global_enable_on_systemd_only(target):
    c, runner, _root = target
    c.enable_user_services(full_plan(user_services=[("pipewire.socket", "socket"), ("wireplumber.service", "service")]))
    assert [line.split("chroot ", 1)[1].split(" ", 1)[1] for line in runner.lines()] == ["systemctl --global enable pipewire.socket", "systemctl --global enable wireplumber.service"]
    runner.commands.clear()
    assert c.enable_user_services(full_plan(scalars={"first_user": "ada", "init": "openrc"})) == [] and runner.commands == []


def test_a_user_service_that_cannot_be_enabled_is_a_warning_not_a_failure(target):
    c, runner, _root = target
    runner.script("*systemctl --global", ("fail", "unit not found", 1))
    warnings = c.enable_user_services(full_plan())
    assert len(warnings) == 1 and "openrazer-daemon.service" in warnings[0]


def test_first_steps_are_written_into_the_first_users_home_and_owned_by_them(target):
    c, runner, root = target
    assert c.write_first_steps(full_plan()) == []
    text = read(root, "home/ada/first-steps.txt")
    assert text.startswith("First steps on your new Gentoo system") and "* Open Polychromatic" in text
    assert any(line.endswith("chown ada: /home/ada/first-steps.txt") for line in runner.lines())
    assert c.write_first_steps(HardwarePlan()) == [] and c.write_first_steps(full_plan(first_steps=[])) == []
    assert c.write_first_steps(full_plan(scalars={})) == []         # nobody to write for
    assert c.write_first_steps(full_plan(scalars={"first_user": "../root"})) == []


def test_run_plan_steps_runs_everything_in_order_and_reports_failures(target):
    c, runner, _root = target
    runner.script("*systemctl --global", ("fail", "boom", 1))
    plan = full_plan(post_steps=[["sh", "-c", "exit 0"]], first_steps=["a"])
    warnings = c.run_plan_steps(plan)
    lines = runner.lines()
    kinds = [("chown" if " chown " in line else "usermod" if "usermod" in line else "getent" if "getent" in line else "systemctl" if "systemctl" in line else "step")
             for line in lines]
    assert kinds.index("chown") < kinds.index("systemctl") < kinds.index("step")
    assert len(warnings) == 1 and "openrazer-daemon.service" in warnings[0]


def test_the_repositories_are_fetched_after_git_is_installed(target):
    c, runner, _root = target
    assert c.prepare_plan_repos(target_manifest(), full_plan()) == []
    lines = runner.lines()
    assert "emerge --noreplace dev-vcs/git" in lines[0] and lines[1].endswith("emaint sync -r guru") and lines[2].endswith("emaint sync -r edorp")
    runner.commands.clear()
    assert c.prepare_plan_repos(target_manifest(), HardwarePlan()) == [] and runner.commands == []


def test_a_repository_that_cannot_be_fetched_degrades_instead_of_failing(target):
    c, runner, _root = target
    runner.script("*emaint sync", ("fail", "fatal: unable to access", 128))
    warnings = c.prepare_plan_repos(target_manifest(), full_plan())
    assert len(warnings) == 1 and "guru" in warnings[0] and "skipped" in warnings[0]


def test_a_plan_saved_by_the_earlier_installer_is_still_read_and_changes_nothing(target):
    c, runner, root = target
    old = HardwarePlan.from_dict({"schema": 1, "early_atoms": ["sys-kernel/linux-firmware"], "files": [["/etc/x.conf", "x\n", 420]], "post_steps": [["true"]]})
    assert old.groups == [] and old.package_keywords == {} and old.repos == [] and 1 in hwplan.READABLE_PLAN_SCHEMAS
    c.write_plan_files(old)
    assert c.run_plan_steps(old) == []
    assert read(root, "etc/x.conf") == "x\n" and runner.lines() == [f"chroot {root} true"]
    assert not (root / "etc/portage/package.accept_keywords").exists()


def test_the_new_fields_survive_a_save_and_a_resume():
    plan = full_plan()
    again = HardwarePlan.from_dict(plan.to_dict())
    assert again.to_dict() == plan.to_dict()
    assert again.groups == ["plugdev", "i2c"] and again.repos == [GURU, EDORP] and again.user_services == [("openrazer-daemon.service", "service")]
    assert again.package_keywords == plan.package_keywords and again.package_unmask == plan.package_unmask and again.first_steps == plan.first_steps
    assert RunnerError  # (imported for the failure tests above)
