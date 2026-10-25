"""NET-03, NET-05, NET-06, I11: what the backend writes and installs for the network choices."""

from __future__ import annotations

from pathlib import Path

import pytest

from gentoo_installerd.configure import Configurer
from gentoo_installerd.configure_network import network_atoms, rewrite_keyfile
from gentoo_installerd.configure_portage import render_make_conf
from gentoo_installerd.proxy import make_conf_settings
from tests.backend.helpers import FakeRunner, manifest

UUID = "11111111-2222-3333-4444-555555555555"
WIFI = f"""[connection]
id=Home
uuid={UUID}
type=wifi
interface-name=wlp4s0
permissions=user:gentoo:;

[wifi]
ssid=Home

[wifi-security]
key-mgmt=wpa-psk
psk=hunter2hunter2
"""


# ------------------------------------------------------------------ NET-03 keep_proxy


def test_credentials_never_reach_make_conf():
    assert make_conf_settings("http://user:secret@proxy:3128") == {}


def test_a_plain_proxy_is_kept_by_default_and_omitted_when_keep_proxy_is_false():
    assert make_conf_settings("http://proxy:3128")["http_proxy"] == "http://proxy:3128"
    assert make_conf_settings("http://proxy:3128", keep=False) == {}


def test_make_conf_follows_keep_proxy():
    m = manifest()
    m.network.proxy = "http://proxy:3128"
    assert "http_proxy" in render_make_conf(m)
    m.network.keep_proxy = False
    assert "proxy" not in render_make_conf(m)


# ------------------------------------------------------------------ NET-05 / NET-06 atoms


def test_netifrc_with_dhcp_pulls_dhcpcd():
    m = manifest()
    m.network.manager = "netifrc"
    assert "net-misc/dhcpcd" in network_atoms(m)
    m.network.dhcp = False
    assert "net-misc/dhcpcd" not in network_atoms(m)


@pytest.mark.parametrize("manager", ["networkmanager", "dhcpcd", "systemd-networkd"])
def test_other_managers_add_no_atoms_by_themselves(manager):
    m = manifest()
    m.network.manager = manager
    assert network_atoms(m) == []


def test_wireless_tools_add_iw_and_wpa_supplicant_and_ppp_only_when_ticked():
    m = manifest()
    assert "net-dialup/ppp" not in network_atoms(m)
    m.network.wireless_tools = True
    assert {"net-wireless/iw", "net-wireless/wpa_supplicant"} <= set(network_atoms(m))
    m.network.ppp = True
    assert "net-dialup/ppp" in network_atoms(m)


def test_the_atoms_reach_the_package_list():
    m = manifest()
    m.network.ppp = True
    assert "net-dialup/ppp" in Configurer(FakeRunner(), "/nonexistent").package_atoms(m)


# ------------------------------------------------------------------ I11 copy_live_wifi


def configurer(tmp_path: Path, profile: str | None) -> tuple[Configurer, list[str]]:
    live = tmp_path / "live"
    live.mkdir()
    if profile is not None:
        (live / "Home.nmconnection").write_text(profile)
    (live / "installer-wifi.nmconnection").write_text(WIFI.replace(UUID, "99999999-2222-3333-4444-555555555555"))
    target = tmp_path / "target"
    target.mkdir()
    messages: list[str] = []
    c = Configurer(FakeRunner(), target, live_network_dir=live)
    c.notify = messages.append
    return c, messages


def test_the_chosen_live_connection_is_copied_with_its_secret(tmp_path):
    c, _ = configurer(tmp_path, WIFI)
    m = manifest()
    m.network.copy_live_wifi = UUID
    c.configure_network(m)
    copied = tmp_path / "target/etc/NetworkManager/system-connections/Home.nmconnection"
    text = copied.read_text()
    assert "psk=hunter2hunter2" in text and "permissions=" not in text and "interface-name=wlp4s0" in text
    assert copied.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "target/etc/NetworkManager/system-connections/installer-wifi.nmconnection").exists()


def test_a_portable_target_drops_the_interface_name(tmp_path):
    c, _ = configurer(tmp_path, WIFI)
    m = manifest()
    m.network.copy_live_wifi = UUID
    m.hardware.target = "portable"
    c.configure_network(m)
    assert "interface-name" not in (tmp_path / "target/etc/NetworkManager/system-connections/Home.nmconnection").read_text()


def test_a_uuid_that_is_gone_is_a_warning_not_an_error(tmp_path):
    c, messages = configurer(tmp_path, None)
    m = manifest()
    m.network.copy_live_wifi = UUID
    c.configure_network(m)
    assert any(message.startswith("warning:") and UUID in message for message in messages)


def test_an_ethernet_profile_is_not_copied_by_this_setting(tmp_path):
    c, _ = configurer(tmp_path, WIFI.replace("type=wifi", "type=ethernet"))
    m = manifest()
    m.network.copy_live_wifi = UUID
    c.configure_network(m)
    assert not (tmp_path / "target/etc/NetworkManager/system-connections/Home.nmconnection").exists()


def test_rewrite_keyfile_drops_wifi_interface_only_when_asked():
    assert "interface-name" in rewrite_keyfile(WIFI).text
    assert "interface-name" in rewrite_keyfile(WIFI, portable=True).text  # the all-profiles import keeps it (earlier behaviour)
    assert "interface-name" not in rewrite_keyfile(WIFI, portable=True, drop_wifi_interface=True).text
