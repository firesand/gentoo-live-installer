"""ACCT backend: hosts, sshd policy, machine-id, locales, groups, privileges, system tools, journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.configure import choice_problems, generated_locales, live_input_problems, render_hosts, render_sshd_config
from gentoo_installerd.runner import RunnerError

from tests.backend.target_helpers import ScriptedRunner, configurer, mark_installed, read, target_manifest


def make(tmp_path: Path, m, runner: ScriptedRunner | None = None, *, dry_run: bool = False):
    zone = tmp_path / "usr/share/zoneinfo/UTC"
    zone.parent.mkdir(parents=True, exist_ok=True)
    zone.write_text("tz")
    runner = runner or ScriptedRunner()
    c = configurer(tmp_path, runner, dry_run=dry_run)
    notes: list[str] = []
    c.notify = notes.append
    return c, runner, notes


# --- ACCT-06 hosts -----------------------------------------------------------------------------------


def test_hosts_without_a_domain_has_no_invented_localdomain():
    assert render_hosts("tux") == "127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\ttux\n"


def test_hosts_with_a_domain_and_extra_lines():
    text = render_hosts("tux", "home.example", ["192.168.1.10   nas  nas.home.example", " "])
    assert text == "127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\ttux.home.example tux\n192.168.1.10 nas nas.home.example\n"


def test_identity_writes_the_hosts_file_from_the_settings(tmp_path):
    m = target_manifest(accounts={"hostname": "tux"}, system={"domain": "lan", "hosts_extra": ["10.0.0.2 printer"]})
    c, _, _ = make(tmp_path, m)
    c.configure_identity(m)
    assert read(tmp_path, "etc/hosts").endswith("127.0.1.1\ttux.lan tux\n10.0.0.2 printer\n")


def test_manifest_validation_rejects_bad_extra_hosts_lines():
    m = target_manifest(system={"hosts_extra": ["not-an-address name"]})
    assert any("Extra hosts" in problem for problem in m.validate())
    assert not any("Extra hosts" in problem for problem in target_manifest(system={"hosts_extra": ["10.0.0.2 printer lp"]}).validate())


# --- I10 / ACCT-08 sshd ------------------------------------------------------------------------------


def test_sshd_policy_with_password_login_only_forbids_root():
    assert render_sshd_config(target_manifest().accounts) == (
        "# Written by the Gentoo LiveCD installer. Change it here, then restart sshd.\nPermitRootLogin no\n")


def test_sshd_policy_for_keys_only():
    accounts = target_manifest(accounts={"ssh_password_login": False}).accounts
    text = render_sshd_config(accounts)
    assert text.splitlines()[1:] == ["PermitRootLogin no", "PasswordAuthentication no", "KbdInteractiveAuthentication no"]


def test_sshd_file_is_written_only_when_ssh_is_ticked(tmp_path):
    m = target_manifest(options={"ssh": True})
    c, _, _ = make(tmp_path, m)
    c.configure_sshd(m)
    assert "PermitRootLogin no" in read(tmp_path, "etc/ssh/sshd_config.d/10-installer.conf")
    other = tmp_path / "off"
    other.mkdir()
    m = target_manifest(options={"ssh": False})
    c, _, _ = make(other, m)
    c.configure_sshd(m)
    assert not (other / "etc/ssh").exists()


@pytest.mark.parametrize("init,service_cmd", [("openrc", "rc-update add sshd default"), ("systemd", "systemctl enable sshd")])
def test_sshd_is_enabled_by_default_and_only_when_ticked(tmp_path, init, service_cmd):
    m = target_manifest(system={"init": init, "stage_variant": f"amd64-{init}"})
    c, runner, _ = make(tmp_path, m)
    c.configure_services(m, phase="base")
    assert any(line.endswith(service_cmd) for line in runner.lines())
    m.options.ssh = False
    c, runner, _ = make(tmp_path, m)
    c.configure_services(m, phase="base")
    assert not any(line.endswith(("sshd default", "enable sshd")) for line in runner.lines())


def test_keys_only_without_a_key_is_refused():
    m = target_manifest(accounts={"ssh_password_login": False})
    assert any("Nobody could log in" in problem for problem in m.validate())
    m.accounts.ssh_authorized_keys = ["ssh-ed25519 AAAA key"]
    assert not any("Nobody could log in" in problem for problem in m.validate())


def test_root_login_over_ssh_is_not_available():
    problems = choice_problems(target_manifest(accounts={"ssh_root_login": True}))
    assert len(problems) == 1 and "Root login over SSH is not available" in problems[0]
    assert choice_problems(target_manifest()) == []


# --- ACCT-05 machine-id ------------------------------------------------------------------------------


def test_systemd_sets_the_machine_id_and_presets_before_its_enables(tmp_path):
    m = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd"})
    c, runner, _ = make(tmp_path, m)
    c.configure_services(m, phase="base")
    lines = runner.lines()
    setup = next(i for i, line in enumerate(lines) if line.endswith("systemd-machine-id-setup"))
    preset = next(i for i, line in enumerate(lines) if line.endswith("systemctl preset-all --preset-mode=enable-only"))
    first_enable = next(i for i, line in enumerate(lines) if "systemctl enable" in line)
    assert setup < preset < first_enable


def test_openrc_runs_dbus_uuidgen_only_once_dbus_is_installed(tmp_path):
    m = target_manifest()
    c, runner, _ = make(tmp_path, m)
    c.configure_services(m, phase="base")
    assert not any("dbus-uuidgen" in line for line in runner.lines())
    mark_installed(tmp_path, "sys-apps/dbus")
    c, runner, _ = make(tmp_path, m)
    c.configure_services(m, phase="base")
    lines = runner.lines()
    assert any(line.endswith("dbus-uuidgen --ensure=/etc/machine-id") for line in lines)
    assert [i for i, line in enumerate(lines) if "dbus-uuidgen" in line][0] < [i for i, line in enumerate(lines) if "rc-update add" in line][0]


def test_a_failing_machine_id_step_is_only_a_warning(tmp_path):
    m = target_manifest(system={"init": "systemd", "stage_variant": "amd64-systemd"})
    runner = ScriptedRunner()
    runner.script("chroot", ("fail", "boom"))
    c, runner, notes = make(tmp_path, m, runner)
    c.configure_services(m, phase="base")
    assert any(note.startswith("warning:") and "machine-id-setup" in note for note in notes)
    assert any("preset-all" in line for line in runner.lines())


# --- ACCT-02 locales ---------------------------------------------------------------------------------


def listing(*names: str) -> str:
    return "\n".join(names) + "\n"


def test_extra_locales_reach_locale_gen_without_duplicates(tmp_path):
    m = target_manifest(system={"locale": "de_DE.UTF-8", "extra_locales": ["fr_FR.UTF-8", "en_US.UTF-8", "de_DE.UTF-8"]})
    assert generated_locales(m.system) == ["de_DE.UTF-8", "fr_FR.UTF-8", "en_US.UTF-8"]
    runner = ScriptedRunner()
    runner.respond("chroot " + str(tmp_path) + " locale -a", listing("C", "POSIX", "de_DE.utf8", "fr_FR.utf8", "en_US.utf8"))
    c, runner, notes = make(tmp_path, m, runner)
    c.configure_identity(m)
    assert read(tmp_path, "etc/locale.gen") == "de_DE.UTF-8 UTF-8\nfr_FR.UTF-8 UTF-8\nen_US.UTF-8 UTF-8\n"
    assert not [note for note in notes if note.startswith("warning")]
    lines = [line.split(" ", 2)[2] for line in runner.lines()]
    assert lines.index("locale-gen") < lines.index("env-update") < lines.index("locale -a")


def test_a_locale_that_was_not_generated_is_a_warning(tmp_path):
    m = target_manifest(system={"extra_locales": ["fr_FR.UTF-8"]})
    runner = ScriptedRunner()
    runner.respond("chroot " + str(tmp_path) + " locale -a", listing("C", "en_US.utf8"))
    c, _, notes = make(tmp_path, m, runner)
    c.configure_identity(m)
    assert any("did not produce fr_FR.UTF-8" in note for note in notes)


def test_locale_a_failing_is_a_warning(tmp_path):
    m = target_manifest()
    runner = ScriptedRunner()
    runner.script("*locale -a", ("fail", "no locale"))
    c, _, notes = make(tmp_path, m, runner)
    c.configure_identity(m)
    assert any("could not be checked" in note for note in notes)


@pytest.mark.parametrize("name", ["C", "POSIX"])
def test_c_and_posix_are_flagged(tmp_path, name):
    m = target_manifest(system={"locale": name})
    c, _, notes = make(tmp_path, m)
    c.configure_identity(m)
    assert any(f"the locale {name} is not a language" in note for note in notes)


# --- ACCT-03 / ACCT-04 accounts, groups, privileges ---------------------------------------------------


def accounts_run(tmp_path, m, runner=None):
    c, runner, notes = make(tmp_path, m, runner)
    c.configure_accounts(m)
    return c, runner, notes


def test_useradd_gets_the_groups_in_order_and_skips_missing_ones(tmp_path):
    m = target_manifest(accounts={"groups": ["wheel", "audio", "video", "usb", "cron"]})
    runner = ScriptedRunner()
    runner.script("*id -u", ("fail", "no such user"))
    runner.script("*getent group audio", "ok")
    runner.script("*getent group video", "ok")
    runner.script("*getent group usb", "ok")
    runner.script("*getent group cron", ("fail", "", 2))
    _, runner, notes = accounts_run(tmp_path, m, runner)
    useradd = next(line for line in runner.lines() if " useradd " in line)
    assert useradd.endswith("--groups wheel,audio,video,usb gentoo")
    assert any("cron" in note and "once their packages are installed" in note for note in notes)


def test_late_groups_join_the_user_when_the_group_appears(tmp_path):
    m = target_manifest(accounts={"groups": ["wheel", "cron"]})
    runner = ScriptedRunner()
    runner.respond("chroot " + str(tmp_path) + " id -nG gentoo", "gentoo wheel\n")
    c, runner, _ = make(tmp_path, m, runner)
    c.add_late_groups(m)
    assert [line for line in runner.lines() if "usermod" in line] == [f"chroot {tmp_path} usermod --append --groups cron gentoo"]


def test_a_group_that_never_exists_is_a_warning_not_an_error(tmp_path):
    m = target_manifest(accounts={"groups": ["wheel", "nonesuch"]})
    runner = ScriptedRunner()
    runner.script("*getent group nonesuch", ("fail", "", 2), ("fail", "", 2))
    runner.respond("chroot " + str(tmp_path) + " id -nG gentoo", "gentoo wheel\n")
    c, runner, notes = make(tmp_path, m, runner)
    c.add_late_groups(m)
    assert not any("usermod" in line for line in runner.lines())
    assert any("nonesuch does not exist" in note or "group nonesuch does not exist" in note for note in notes)


def test_root_modes_set_the_root_password(tmp_path):
    m = target_manifest(accounts={"root_mode": "same", "password": "user-password", "root_password": "ignored-one"})
    _, runner, _ = accounts_run(tmp_path, m)
    assert runner.inputs[[line.endswith("chpasswd") for line in runner.lines()].index(True)] == "gentoo:user-password\nroot:user-password\n"
    m = target_manifest(accounts={"root_mode": "separate", "root_password": "root-secret-1"})
    _, runner, _ = accounts_run(tmp_path, m)
    assert "root:root-secret-1\n" in [i for i in runner.inputs if i][0]


def test_locked_root_has_no_root_password_and_is_locked(tmp_path):
    m = target_manifest(accounts={"root_mode": "locked"})
    _, runner, _ = accounts_run(tmp_path, m)
    payload = [i for i in runner.inputs if i][0]
    assert "root:" not in payload
    assert any(line.endswith("passwd --lock root") for line in runner.lines())


def test_locked_root_needs_wheel_and_a_tool_that_does_not_ask_for_the_root_password():
    assert any("wheel group" in p for p in choice_problems(target_manifest(accounts={"root_mode": "locked", "groups": ["audio"]})))
    assert any("su needs the root password" in p for p in choice_problems(target_manifest(accounts={"root_mode": "locked", "privilege": "su"})))
    assert choice_problems(target_manifest(accounts={"root_mode": "locked"})) == []


@pytest.mark.parametrize("tool,path,content", [("sudo", "etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n"), ("doas", "etc/doas.conf", "permit persist :wheel\n")])
def test_sudo_and_doas_get_a_wheel_rule(tmp_path, tool, path, content):
    m = target_manifest(accounts={"privilege": tool})
    c, _, _ = make(tmp_path, m)
    c.configure_privileges(m)
    assert read(tmp_path, path) == content


@pytest.mark.parametrize("tool", ["run0", "su"])
def test_run0_and_su_write_no_rule_file(tmp_path, tool):
    m = target_manifest(accounts={"privilege": tool}, system={"init": "systemd", "stage_variant": "amd64-systemd"})
    c, _, _ = make(tmp_path, m)
    c.configure_privileges(m)
    assert not (tmp_path / "etc/sudoers.d").exists() and not (tmp_path / "etc/doas.conf").exists()


def test_privilege_tools_install_the_right_packages(tmp_path):
    def atoms(**accounts):
        c, _, _ = make(tmp_path, target_manifest(accounts=accounts, system={"init": "systemd", "stage_variant": "amd64-systemd"}))
        return c.system_tool_atoms(target_manifest(accounts=accounts, system={"init": "systemd", "stage_variant": "amd64-systemd"}))

    assert atoms(privilege="sudo")[0] == "app-admin/sudo"
    assert atoms(privilege="doas")[0] == "app-admin/doas"
    assert atoms(privilege="run0")[0] == "sys-auth/polkit"
    assert "app-admin/sudo" not in atoms(privilege="su") and "app-admin/doas" not in atoms(privilege="su")


# --- ACCT-07 system tools ----------------------------------------------------------------------------


def tools(tmp_path, init="openrc", **options):
    m = target_manifest(system={"init": init, "stage_variant": f"amd64-{init}"}, options=options)
    c, runner, notes = make(tmp_path, m)
    return m, c, runner, notes


def test_the_default_tool_atoms_are_what_they_were(tmp_path):
    m, c, _, _ = tools(tmp_path)
    assert c.system_tool_atoms(m) == ["app-admin/sudo", "net-misc/openssh", "net-misc/chrony", "sys-process/cronie", "app-shells/bash-completion",
                                      "app-admin/syslog-ng", "app-admin/logrotate"]


@pytest.mark.parametrize("logger,atoms,service", [("sysklogd", ["app-admin/sysklogd"], "sysklogd"),
                                                  ("syslog-ng", ["app-admin/syslog-ng", "app-admin/logrotate"], "syslog-ng"),
                                                  ("metalog", ["app-admin/metalog"], "metalog")])
def test_each_openrc_logger_has_its_package_and_service(tmp_path, logger, atoms, service):
    m, c, _, _ = tools(tmp_path, logger=logger, cron="none")
    assert all(atom in c.system_tool_atoms(m) for atom in atoms)
    assert ("sys-process/cronie" in c.system_tool_atoms(m)) == (logger == "syslog-ng")  # sysklogd rotates itself (its logrotate USE flag is off), metalog too
    assert (service, "default", False) in c.service_specs(m)


def test_logger_none_installs_no_logger(tmp_path):
    m, c, _, _ = tools(tmp_path, logger="none", cron="none")
    assert not any("log" in atom for atom in c.system_tool_atoms(m))
    assert not any(name in {"sysklogd", "syslog-ng", "metalog"} for name, _, _ in c.service_specs(m))


@pytest.mark.parametrize("cron,atom", [("cronie", "sys-process/cronie"), ("dcron", "sys-process/dcron"), ("fcron", "sys-process/fcron"), ("bcron", "sys-process/bcron")])
@pytest.mark.parametrize("init", ["openrc", "systemd"])
def test_each_cron_daemon_has_its_package_and_service(tmp_path, cron, atom, init):
    m, c, _, _ = tools(tmp_path, init, cron=cron, logger="none")
    assert atom in c.system_tool_atoms(m)
    assert any(name == cron for name, _, _ in c.service_specs(m))
    assert not any(a in c.system_tool_atoms(m) for a in {"sys-process/cronie", "sys-process/dcron", "sys-process/fcron", "sys-process/bcron"} - {atom})


def test_the_cron_daemons_first_run_steps(tmp_path):
    for cron, line in (("dcron", "crontab /etc/crontab"), ("fcron", "emerge --config sys-process/fcron")):
        m, c, runner, _ = tools(tmp_path, cron=cron)
        c.configure_cron_daemon(m)
        assert runner.lines() == [f"chroot {tmp_path} {line}"]
    m, c, runner, _ = tools(tmp_path, cron="cronie")
    c.configure_cron_daemon(m)
    assert runner.lines() == []


def test_a_failing_cron_first_run_step_is_a_warning(tmp_path):
    m, c, runner, notes = tools(tmp_path, cron="dcron")
    runner.script("chroot", ("fail", "no crontab"))
    c.configure_cron_daemon(m)
    assert notes and notes[0].startswith("warning:")


def test_cron_none_with_syslog_ng_still_pulls_cronie_for_logrotate(tmp_path):
    m, c, _, _ = tools(tmp_path, cron="none", logger="syslog-ng")
    assert c.cron_daemon(m) == "cronie" and "sys-process/cronie" in c.system_tool_atoms(m)
    m, c, _, _ = tools(tmp_path, "systemd", cron="none", logger="syslog-ng")
    assert c.cron_daemon(m) == ""


@pytest.mark.parametrize("timesync,atom,service", [("chrony", "net-misc/chrony", "chronyd"), ("timesyncd", None, "systemd-timesyncd"), ("none", None, None)])
def test_time_sync_choices(tmp_path, timesync, atom, service):
    m, c, _, _ = tools(tmp_path, "systemd", timesync=timesync)
    assert ("net-misc/chrony" in c.system_tool_atoms(m)) == (atom is not None)
    names = [name for name, _, _ in c.service_specs(m)]
    assert {"chronyd", "systemd-timesyncd"} & set(names) == ({service} if service else set())


def test_optional_tools(tmp_path):
    m, c, _, _ = tools(tmp_path, gentoolkit=True, eclean_kernel=True, mlocate=True, shell_completion=False)
    atoms = c.system_tool_atoms(m)
    assert {"app-portage/gentoolkit", "app-admin/eclean-kernel", "sys-apps/mlocate"} <= set(atoms) and "app-shells/bash-completion" not in atoms
    m, c, _, _ = tools(tmp_path, file_indexing=True)
    assert "sys-apps/mlocate" in c.system_tool_atoms(m)  # the older setting still works


def test_essential_atoms_carry_the_tool_atoms(tmp_path):
    m, c, _, _ = tools(tmp_path, gentoolkit=True)
    assert "app-portage/gentoolkit" in c.essential_atoms(m)


def test_persistent_journal_creates_the_directory_on_systemd_only(tmp_path):
    m, c, _, _ = tools(tmp_path, "systemd", journal_persistent=True)
    c.configure_journal(m)
    assert (tmp_path / "var/log/journal").is_dir()
    other = tmp_path / "off"
    other.mkdir()
    m, c, _, _ = tools(other, "systemd", journal_persistent=False)
    c.configure_journal(m)
    assert not (other / "var/log/journal").exists()
    assert any("needs systemd" in p for p in choice_problems(target_manifest(options={"journal_persistent": True})))


def test_dry_run_writes_nothing_for_the_new_steps(tmp_path):
    m = target_manifest(options={"journal_persistent": True}, system={"init": "systemd", "stage_variant": "amd64-systemd"})
    c, _, _ = make(tmp_path, m, dry_run=True)
    c.configure_sshd(m)
    c.configure_journal(m)
    assert not (tmp_path / "etc/ssh").exists() and not (tmp_path / "var/log").exists()


def test_preflight_lists_the_choice_problems(tmp_path):
    m = target_manifest(accounts={"ssh_root_login": True})
    assert any("Root login over SSH" in p for p in live_input_problems(m, root=tmp_path))


def test_cron_daemons_without_a_service_script_are_refused_not_ignored():
    assert any("bcron is not available yet" in p for p in choice_problems(target_manifest(options={"cron": "bcron"})))
    assert any("fcron is not available with systemd" in p for p in choice_problems(target_manifest(options={"cron": "fcron"}, system={"init": "systemd", "stage_variant": "amd64-systemd"})))
    assert choice_problems(target_manifest(options={"cron": "fcron"})) == []
