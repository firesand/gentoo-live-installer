"""D6, D7 and I16: the daemon's service files for OpenRC and systemd."""
from __future__ import annotations

import configparser
import re
import subprocess

from .conftest import ROOT

FILES = ROOT / "packaging/gentoo-overlay/app-admin/gentoo-live-installer/files"


def openrc_variables(text: str) -> dict[str, str]:
    return {m[1]: m[2] for m in re.finditer(r'^([a-z_]+)="([^"]*)"', text, re.M)} | {m[1]: m[2] for m in re.finditer(r"^([a-z_]+)=([^\"\s]+)$", text, re.M)}


def systemd_unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string((FILES / "gentoo-installerd.service").read_text())
    return parser


def test_openrc_script_is_valid_and_logs_to_a_file() -> None:
    path = FILES / "gentoo-installerd.initd"
    assert subprocess.run(["sh", "-n", str(path)], capture_output=True, check=False).returncode == 0
    text = path.read_text()
    values = openrc_variables(text)
    assert values["command"] == "/usr/bin/gentoo-installerd"
    assert values["output_log"] == values["error_log"] == "/var/log/gentoo-installerd.log"
    assert values["command_background"] == "true" and values["pidfile"] == "/run/${RC_SVCNAME}.pid"


def test_openrc_script_runs_as_root_without_dbus() -> None:
    text = (FILES / "gentoo-installerd.initd").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "command_user" not in code, "the daemon runs as root:root; the socket directory is chowned by the daemon itself"
    assert not re.search(r"\b(need|want|use)\s+[^\n]*\bdbus\b", code)
    assert re.search(r"\buse logger\b", code) and re.search(r"\bafter udev\b", code)


def test_stop_behaviour_is_consistent_between_init_systems() -> None:
    initd = openrc_variables((FILES / "gentoo-installerd.initd").read_text())
    unit = systemd_unit()
    assert initd["retry"] == "TERM/60/KILL/5"
    assert unit["Service"]["TimeoutStopSec"] == "60"
    assert unit["Service"]["KillMode"] == "mixed", "SIGTERM goes to the daemon, which cancels an install; stragglers get SIGKILL"


def test_systemd_unit_has_no_dbus_dependency_and_no_group() -> None:
    unit = systemd_unit()
    assert "dbus" not in " ".join(unit["Unit"].values()).lower()
    assert "Requires" not in unit["Unit"] and "BindsTo" not in unit["Unit"]
    assert "Group" not in unit["Service"] and "User" not in unit["Service"]
    assert "NetworkManager.service" in unit["Unit"]["After"]
    assert unit["Service"]["RuntimeDirectory"] == "gentoo-installer"
    assert unit["Service"]["Restart"] == "on-failure" and unit["Service"]["ExecStart"] == "/usr/bin/gentoo-installerd"
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_conf_d_cleans_up_stragglers_and_documents_arguments() -> None:
    values = openrc_variables((FILES / "gentoo-installerd.confd").read_text())
    assert values["rc_cgroup_cleanup"] == "YES"
    assert "command_args" in (FILES / "gentoo-installerd.confd").read_text()


def test_the_daemon_group_and_socket_directory_agree_with_the_packaging() -> None:
    """The socket directory is root:gentoo-installer 0750; the group comes from acct-group/gentoo-installer."""
    server = (ROOT / "src/gentoo_installerd/server.py").read_text()
    assert re.search(r'SOCKET_GROUP\s*=\s*"gentoo-installer"', server)
    assert (ROOT / "packaging/gentoo-overlay/acct-group/gentoo-installer").is_dir()
