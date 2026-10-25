"""NET-01..06, I9, I11 in the GUI: the connection card, the Wi-Fi flow, the proxy, the network tab and the remote-access dialog (fakes only)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer import netclient, remote_cli
from gentoo_installer.daemon_client import DaemonState
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.pages import network_tab
from gentoo_installer.pages.network_card import NetworkCard
from gentoo_installer.pages.readiness import ReadinessPage, proxy_has_credentials
from gentoo_installer.pages.remote_dialog import RemoteAccessDialog
from tests.ui.conftest import sample_info

PASSWORD = "correct-horse-battery"
UUID = "11111111-2222-3333-4444-555555555555"

STATUS_OK = {"ok": True, "problem": "", "message": "", "wifi_present": True, "wifi_enabled": True, "rfkill_hard": False, "steps": [],
             "devices": [{"name": "enp5s0", "kind": "ethernet", "state": "connected", "connection": "Wired connection 1", "ipv4": ["192.168.1.20/24"]},
                         {"name": "wlp4s0", "kind": "wifi", "state": "disconnected", "connection": "", "ipv4": []}]}
STATUS_OFFLINE = {**STATUS_OK, "problem": "no_connection", "message": "Wi-Fi is not connected yet. Pick your network from the list and type its password.",
                  "devices": [{"name": "wlp4s0", "kind": "wifi", "state": "disconnected", "connection": "", "ipv4": []}],
                  "steps": [{"key": "link", "label": "A network adapter with a link", "ok": True, "detail": ""},
                            {"key": "address", "label": "An address from the router", "ok": False, "detail": ""},
                            {"key": "route", "label": "A route to the internet", "ok": None, "detail": ""}]}
NETWORKS = [{"ssid": "Home", "signal": 80, "security": "wpa-psk", "in_use": False, "known": False, "band": "5"},
            {"ssid": "Guest", "signal": 60, "security": "open", "in_use": False, "known": False, "band": "2.4"},
            {"ssid": "Office", "signal": 50, "security": "enterprise", "in_use": False, "known": False, "band": "5"}]


class FakeService:
    def __init__(self, **answers) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answers = {"network_status": STATUS_OFFLINE, "wifi_scan": {"ok": True, "networks": NETWORKS},
                        "wifi_connect": {"ok": True, "message": "Connected to Home.", "uuid": UUID, "ssid": "Home"},
                        "wifi_live_connections": {"ok": True, "connections": []}, "network_static": {"ok": True, "message": "The address was applied."},
                        "wifi_radio": {"ok": True}, **answers}

    def __call__(self, action, payload):
        self.calls.append((action, dict(payload)))
        answer = self.answers[action]
        return answer(payload) if callable(answer) else dict(answer)

    def payloads(self, action):
        return [payload for name, payload in self.calls if name == action]


@pytest.fixture
def card(qtbot):
    def build(service=None, manifest=None, secret=""):
        widget = NetworkCard(manifest if manifest is not None else InstallManifest(), caller=service or FakeService(), synchronous=True,
                             secret_reader=lambda uuid: secret)
        qtbot.addWidget(widget)
        widget.show()
        return widget
    return build


# ------------------------------------------------------------------ NET-01


def test_offline_card_lists_adapters_and_explains_with_a_why_not(card):
    widget = card()
    widget.refresh()
    assert widget.adapters.text() == "Wi-Fi: wlp4s0 (not connected)"
    assert "Pick your network" in widget.problem.text() and widget.problem.isVisibleTo(widget)
    widget.why_button.setChecked(True)
    lines = widget.why_box.text().splitlines()
    assert lines == ["✓ A network adapter with a link", "× An address from the router", "– A route to the internet"]


def test_online_card_has_no_problem_and_lists_wired_and_wifi(card):
    widget = card(FakeService(network_status=STATUS_OK))
    widget.refresh()
    assert widget.adapters.text() == "Wired: enp5s0 (connected, 192.168.1.20)\nWi-Fi: wlp4s0 (not connected)"
    assert not widget.problem.isVisibleTo(widget) and not widget.why_button.isVisibleTo(widget) and not widget.wifi_box.isVisibleTo(widget)


def test_only_loopback_gets_the_hardware_hint(card):
    widget = card(FakeService(network_status={**STATUS_OFFLINE, "devices": [], "wifi_present": False, "problem": "no_device",
                                              "message": "No network adapter was found, only the computer itself. The adapter or its driver is missing."}))
    widget.refresh()
    assert widget.adapters.text() == "No network adapter was found." and "driver" in widget.problem.text()


def test_the_card_does_nothing_without_a_service(qtbot):
    widget = NetworkCard(InstallManifest(), synchronous=True)
    qtbot.addWidget(widget)
    widget.refresh()
    assert widget.status is None and not widget.available


# ------------------------------------------------------------------ NET-02


def test_offline_with_wifi_scans_and_lists_the_networks_with_signal(card):
    widget = card()
    widget.refresh()
    assert widget.wifi_box.isVisibleTo(widget)
    assert widget.network_list.count() == 3 and "Home" in widget.network_list.item(0).text() and "WPA2" in widget.network_list.item(0).text()


def test_connecting_sends_the_password_to_the_service_and_remembers_the_network(card):
    manifest = InstallManifest()
    service = FakeService()
    widget = card(service, manifest)
    widget.refresh()
    rechecks = []
    widget.recheckRequested.connect(lambda: rechecks.append(True))
    widget.network_list.setCurrentRow(0)
    assert widget.password.isEnabled() and widget.connect_button.isEnabled()
    widget.password.setText(PASSWORD)
    widget.connect_selected()
    assert service.payloads("wifi_connect") == [{"ssid": "Home", "password": PASSWORD}]
    assert manifest.network.copy_live_wifi == UUID and manifest.network.wifi_password == ""  # the secret stays in the live profile
    assert widget.password.text() == "" and rechecks and "Connected" in widget.wifi_result.text()


def test_remember_unticked_keeps_the_manifest_alone(card):
    manifest = InstallManifest()
    widget = card(FakeService(), manifest)
    widget.refresh()
    widget.remember.setChecked(False)
    widget.network_list.setCurrentRow(0)
    widget.password.setText(PASSWORD)
    widget.connect_selected()
    assert manifest.network.copy_live_wifi == ""


def test_an_open_network_needs_no_password_and_mentions_the_login_page(card):
    service = FakeService()
    widget = card(service)
    widget.refresh()
    widget.network_list.setCurrentRow(1)
    assert not widget.password.isEnabled() and widget.connect_button.isEnabled()
    assert "browser to log in" in widget.open_note.text()
    widget.connect_selected()
    assert service.payloads("wifi_connect") == [{"ssid": "Guest", "password": ""}]


def test_enterprise_networks_go_to_the_settings_tool(card):
    widget = card()
    widget.refresh()
    widget.network_list.setCurrentRow(2)
    assert not widget.connect_button.isEnabled() and "More network settings" in widget.open_note.text()
    asked = []
    widget.networkSettingsRequested.connect(lambda: asked.append(True))
    widget.settings_button.click()
    assert asked


def test_a_wrong_password_shows_the_message_and_changes_nothing(card):
    manifest = InstallManifest()
    widget = card(FakeService(wifi_connect={"ok": False, "reason": "wrong_password", "message": "The password was not accepted. Check it and try again."}), manifest)
    widget.refresh()
    widget.network_list.setCurrentRow(0)
    widget.password.setText(PASSWORD)
    widget.connect_selected()
    assert "not accepted" in widget.wifi_result.text() and manifest.network.copy_live_wifi == ""
    assert widget.connect_button.isEnabled()
    assert PASSWORD not in widget.wifi_result.text()


def test_a_blocked_radio_gets_the_hint_and_a_switch(card):
    soft = card(FakeService(network_status={**STATUS_OFFLINE, "wifi_enabled": False}))
    soft.refresh()
    assert soft.rfkill_hint.isVisibleTo(soft) and soft.radio_button.isVisibleTo(soft) and not soft.scan_button.isEnabled()
    hard = card(FakeService(network_status={**STATUS_OFFLINE, "rfkill_hard": True}))
    hard.refresh()
    assert "Wi-Fi key" in hard.rfkill_hint.text() and not hard.radio_button.isVisibleTo(hard)


def test_no_password_is_ever_put_into_the_manifest_text_by_connecting(card):
    manifest = InstallManifest()
    widget = card(FakeService(), manifest)
    widget.refresh()
    widget.network_list.setCurrentRow(0)
    widget.password.setText(PASSWORD)
    widget.connect_selected()
    assert PASSWORD not in json.dumps(manifest.to_dict())


# ------------------------------------------------------------------ I11


def live(secret_stored, uuid=UUID, ssid="Home"):
    return {"ok": True, "connections": [{"uuid": uuid, "ssid": ssid, "active": True, "secret_stored": secret_stored}]}


def test_the_active_connection_with_a_stored_secret_is_copied_by_default(card):
    manifest = InstallManifest()
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(True)), manifest)
    widget.refresh()
    assert manifest.network.copy_live_wifi == UUID and not widget.again_box.isVisibleTo(widget)


def test_a_portable_target_does_not_copy_by_default(card):
    manifest = InstallManifest()
    manifest.hardware.target = "portable"
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(True)), manifest)
    widget.refresh()
    assert manifest.network.copy_live_wifi == ""


def test_a_secret_in_the_keyring_is_read_without_asking(card):
    manifest = InstallManifest()
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(False)), manifest, secret="from-the-keyring")
    widget.refresh()
    assert (manifest.network.wifi_ssid, manifest.network.wifi_password) == ("Home", "from-the-keyring") and not widget.again_box.isVisibleTo(widget)


def test_when_the_secret_cannot_be_read_the_card_asks_once(card):
    manifest = InstallManifest()
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(False)), manifest)
    widget.refresh()
    assert widget.again_box.isVisibleTo(widget)
    assert widget.again_label.text() == "Type the password of Home once more so the installed system can connect."
    widget.again_password.setText(PASSWORD)
    assert (manifest.network.wifi_ssid, manifest.network.wifi_password) == ("Home", PASSWORD)


def test_an_earlier_choice_is_not_overwritten(card):
    manifest = InstallManifest()
    manifest.network.copy_live_wifi = "99999999-2222-3333-4444-555555555555"
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(True)), manifest)
    widget.refresh()
    assert manifest.network.copy_live_wifi.startswith("99999999")


def test_other_managers_get_no_wifi_carry_over(card):
    manifest = InstallManifest()
    manifest.network.manager = "dhcpcd"
    widget = card(FakeService(network_status=STATUS_OK, wifi_live_connections=live(True)), manifest)
    widget.refresh()
    assert manifest.network.copy_live_wifi == ""


# ------------------------------------------------------------------ NET-04


def fill_static(widget, address="192.168.1.50/24", gateway="192.168.1.1"):
    widget.static_check.setChecked(True)
    widget.address.setText(address)
    widget.gateway.setText(gateway)


def test_a_fixed_address_is_applied_and_copied_to_the_installed_system(card):
    manifest = InstallManifest()
    service = FakeService(network_status=STATUS_OK)
    widget = card(service, manifest)
    widget.refresh()
    fill_static(widget)
    widget.dns_preset.setCurrentIndex(1)  # Cloudflare
    widget.apply_static()
    assert service.payloads("network_static") == [{"address": "192.168.1.50/24", "gateway": "192.168.1.1", "dns": ["1.1.1.1", "1.0.0.1"],
                                                  "connection": "Wired connection 1"}]
    network = manifest.network
    assert network.live_static.address == "192.168.1.50/24" and network.live_static.dns == ["1.1.1.1", "1.0.0.1"]
    assert (network.dhcp, network.address, network.gateway, network.dns) == (False, "192.168.1.50/24", "192.168.1.1", ["1.1.1.1", "1.0.0.1"])
    assert manifest.validate_sections()["network"] == []


def test_the_installed_system_is_left_alone_when_not_ticked(card):
    manifest = InstallManifest()
    widget = card(FakeService(network_status=STATUS_OK), manifest)
    widget.refresh()
    fill_static(widget)
    widget.same_on_installed.setChecked(False)
    widget.apply_static()
    assert manifest.network.live_static.address and manifest.network.dhcp is True and manifest.network.address == ""


def test_bad_addresses_are_refused_before_anything_is_sent(card):
    service = FakeService(network_status=STATUS_OK)
    widget = card(service)
    widget.refresh()
    fill_static(widget, gateway="10.9.9.9")
    widget.apply_static()
    assert "same network" in widget.static_result.text() and service.payloads("network_static") == []
    assert "same network" in widget.validate()
    widget.address.setText("192.168.1.50")
    widget.apply_static()
    assert "prefix" in widget.static_result.text()


def test_own_dns_servers_are_validated(card):
    service = FakeService(network_status=STATUS_OK)
    widget = card(service)
    fill_static(widget)
    widget.dns_preset.setCurrentIndex(widget.dns_preset.count() - 1)
    widget.dns_own.setText("not-an-address")
    assert "not a DNS server" in widget.static_error()
    widget.dns_own.setText("9.9.9.9, 1.1.1.1")
    assert widget.dns_servers() == ["9.9.9.9", "1.1.1.1"] and widget.static_error() == ""


def test_back_to_automatic_clears_the_live_setting(card):
    manifest = InstallManifest()
    service = FakeService(network_status=STATUS_OK)
    widget = card(service, manifest)
    widget.refresh()
    fill_static(widget)
    widget.apply_static()
    widget.use_dhcp()
    assert service.payloads("network_static")[-1] == {"dhcp": True, "connection": "Wired connection 1"}
    assert not manifest.network.live_static.is_set()


def test_a_loaded_fixed_address_shows_in_the_form(card):
    manifest = InstallManifest()
    manifest.network.live_static.address, manifest.network.live_static.gateway = "10.0.0.5/24", "10.0.0.1"
    manifest.network.live_static.dns = ["9.9.9.9", "149.112.112.112"]
    widget = card(FakeService(), manifest)
    widget.load_manifest()
    assert widget.static_check.isChecked() and widget.address.text() == "10.0.0.5/24" and widget.dns_preset.currentText().startswith("Quad9")


# ------------------------------------------------------------------ NET-03 / NET-06 (the page)


def page_with(qtbot, manifest=None, service=None):
    page = ReadinessPage(manifest if manifest is not None else InstallManifest())
    qtbot.addWidget(page)
    page.network_card.caller = service
    page.network_card.synchronous = True
    return page


def test_proxy_credentials_are_detected_like_the_manifest_does():
    assert proxy_has_credentials("http://alice:pw@proxy:3128") and proxy_has_credentials("http://john@corp.com:secret@proxy:3128")
    assert not proxy_has_credentials("http://proxy:3128") and not proxy_has_credentials("")


def test_keep_proxy_follows_the_box_and_is_forced_off_with_credentials(qtbot):
    manifest = InstallManifest()
    page = page_with(qtbot, manifest)
    page.proxy.setText("http://proxy:3128")
    assert page.keep_proxy.isEnabled() and page.keep_proxy.isChecked()
    page.save_manifest()
    assert manifest.network.keep_proxy is True
    page.keep_proxy.setChecked(False)
    page.save_manifest()
    assert manifest.network.keep_proxy is False
    page.proxy.setText("http://alice:pw@proxy:3128")
    assert not page.keep_proxy.isEnabled() and not page.keep_proxy.isChecked() and "installation only" in page.keep_proxy_note.text()
    page.save_manifest()
    assert manifest.network.keep_proxy is False and manifest.network.proxy == "http://alice:pw@proxy:3128"
    page.proxy.setText("http://proxy:3128")
    assert page.keep_proxy.isChecked() and page.keep_proxy.isEnabled()  # the default is back without the password


def test_the_proxy_help_text_matches_the_behaviour(qtbot):
    from gentoo_installer.pages import readiness
    assert "kept in the installed system's Portage settings" in readiness.PROXY_HELP and "not stored" not in readiness.PROXY_HELP


def test_the_proxy_lives_in_a_more_options_section(qtbot):
    page = page_with(qtbot)
    assert page.proxy_section.title == "My network needs a proxy"
    page.set_advanced(True)
    assert page.proxy_section.is_open()


def test_the_remote_button_and_the_card_need_the_service(make_window):
    info = sample_info()
    info.daemon = DaemonState("running", features=frozenset({"network.v1", "remote.v1"}))
    window = make_window(info=info)
    page = window.readiness
    page.network_card.synchronous = True
    page.network_card.caller = FakeService(network_status=STATUS_OK)
    info.daemon = DaemonState("running", features=frozenset({"network.v1", "remote.v1"}))
    window.apply_info(info)
    assert not page.remote_button.isHidden() and page.network_card.status is not None and not page.network_card.isHidden()
    assert "Wired: enp5s0" in page.rows["network"].detail.text()
    info.daemon = DaemonState("running", features=frozenset())
    window.apply_info(info)
    assert page.remote_button.isHidden()


def test_development_mode_row_is_hidden(make_window):
    window = make_window()
    assert window.readiness.rows["service"].isHidden()


def test_each_note_is_shown_once_and_the_windows_note_has_its_own_title(make_window):
    info = sample_info(warnings=[
        {"code": "hw.fw_win_entry", "severity": "info", "message": "Windows is installed on this PC. Install next to it only if you have a backup."},
        {"code": "hw.gpu_a", "severity": "info", "message": "Same note twice."}, {"code": "hw.gpu_b", "severity": "info", "message": "Same note twice."},
        {"code": "hw.bat_ac_offline", "severity": "warning", "message": "The charger is not connected."}])
    window = make_window(info=info)
    window.apply_info(info)
    page = window.readiness
    rows = [page.notes_box.itemAt(i).widget() for i in range(page.notes_box.count())]
    assert sorted(row.detail.text() for row in rows) == ["Same note twice.", "Windows is installed on this PC. Install next to it only if you have a backup."]
    assert next(row for row in rows if row.detail.text().startswith("Windows")).title.text() == "Windows"
    assert page.charger_banner.text() == "The charger is not connected." and not page.charger_banner.isHidden()
    assert "certificates" in page.clock_explain.text()


# ------------------------------------------------------------------ NET-05 / NET-06 (the network tab)


def customize(make_window, info=None, manifest=None):
    window = make_window(info=info, manifest=manifest)
    window._show_page(4)
    window.customize.tabs.setCurrentIndex(2)  # the Network tab
    return window, window.customize


def test_guided_shows_one_line_and_advanced_shows_everything(make_window):
    window, page = customize(make_window)
    assert page.network_summary.text() == "Network: NetworkManager (recommended, handles Wi-Fi and cable)"
    assert not page.network_more.is_open() and page.network_change.isVisibleTo(page)
    page.network_change.click()
    assert page.network_more.is_open()
    page.set_advanced(True)
    assert page.network_more.is_open() and not page.network_change.isVisibleTo(page)


def test_manager_labels_use_plain_words(make_window):
    window, page = customize(make_window)
    labels = [page.network_manager.itemText(i) for i in range(page.network_manager.count())]
    assert labels == ["NetworkManager - easiest for laptops and Wi-Fi", "dhcpcd - wired and simple (no Wi-Fi manager)",
                      "netifrc - OpenRC, one setting per network card", "systemd-networkd - wired only"]


def test_the_interface_is_prefilled_with_the_detected_wired_card(make_window):
    from gentoo_installer.hardware import NicInfo
    info = sample_info()
    info.hardware = {"nics": [{"iface": "wlp4s0", "kind": "wifi"}, {"iface": "enp5s0", "kind": "ethernet", "carrier": True, "primary": True}]}
    window, page = customize(make_window, info)
    assert page.detected_adapters() == (["wlp4s0", "enp5s0"], "enp5s0")
    index = page.network_manager.findData("dhcpcd")
    page.network_manager.setCurrentIndex(index)
    page.network_manager.activated.emit(index)
    assert page.interface.text() == "enp5s0"
    assert page.wireless_tools.isChecked()  # a Wi-Fi card without NetworkManager gets its tools
    assert "WEP" in page.network_note.text()
    assert NicInfo


def test_a_portable_target_with_a_per_card_manager_is_warned(make_window):
    window, page = customize(make_window)
    page.network_more.set_open(True)
    page.hardware.target.set_value("portable")
    index = page.network_manager.findData("netifrc")
    page.network_manager.setCurrentIndex(index)
    page.network_manager.activated.emit(index)
    assert "use NetworkManager for a system you move around" in page.portable_warning.text() and page.portable_warning.isVisibleTo(page)
    index = page.network_manager.findData("networkmanager")
    page.network_manager.setCurrentIndex(index)
    page.network_manager.activated.emit(index)
    assert not page.portable_warning.isVisibleTo(page)


def test_ppp_and_wireless_tools_reach_the_manifest_only_when_ticked(make_window):
    window, page = customize(make_window)
    page.save_manifest()
    assert (window.manifest.network.ppp, window.manifest.network.wireless_tools) == (False, False)
    page.ppp.setChecked(True)
    page.wireless_tools.setChecked(True)
    page.save_manifest()
    assert (window.manifest.network.ppp, window.manifest.network.wireless_tools) == (True, True)


def test_the_proxy_is_shown_read_only_here_and_never_changed_by_this_tab(make_window):
    manifest = InstallManifest()
    manifest.network.proxy = "http://proxy:3128"
    manifest.network.keep_proxy = False
    window, page = customize(make_window, manifest=manifest)
    assert page.proxy.isReadOnly() and page.proxy.text() == "http://proxy:3128"
    page.save_manifest()
    assert window.manifest.network.proxy == "http://proxy:3128" and window.manifest.network.keep_proxy is False


# ------------------------------------------------------------------ I9 (dialog and console command)

STATUS_OFF = {"ok": True, "message": "", "sshd_running": True, "enabled": False, "user": "gentoo", "port": 22, "addresses": ["192.168.1.20"],
              "fingerprints": [{"type": "ED25519", "sha256": "SHA256:abc"}]}
STATUS_ON = {**STATUS_OFF, "enabled": True, "password": "maple-river-stone42", "generated": True}


def test_describe_off_names_both_ways_in():
    lines = remote_cli.describe(STATUS_OFF)
    assert lines[0].startswith("Remote access is OFF") and "gentoo-remote-access on" in lines[1]
    assert any("sudo passwd root" in line for line in lines)


def test_describe_on_prints_the_ssh_line_password_and_fingerprints():
    lines = remote_cli.describe(STATUS_ON, "maple-river-stone42")
    assert "    ssh gentoo@192.168.1.20" in lines and "The password is: maple-river-stone42" in lines
    assert any("SHA256:abc" in line for line in lines)
    assert "maple-river-stone42" not in "\n".join(remote_cli.describe(STATUS_ON))  # status alone never prints it


def test_the_console_command_talks_to_the_service(monkeypatch, capsys):
    calls = []

    def fake_call(socket_path, action, payload=None, **kwargs):
        calls.append((action, payload))
        return {"remote_enable": STATUS_ON, "remote_disable": STATUS_OFF, "remote_status": STATUS_OFF}[action]
    monkeypatch.setattr(netclient, "call", fake_call)
    import io
    out = io.StringIO()
    assert remote_cli.main(["on"], out=out) == 0
    assert calls == [("remote_enable", {"password": ""})] and "ssh gentoo@192.168.1.20" in out.getvalue() and "maple-river-stone42" in out.getvalue()
    out = io.StringIO()
    assert remote_cli.main([], out=out) == 0 and calls[-1][0] == "remote_status" and "OFF" in out.getvalue()
    out = io.StringIO()
    assert remote_cli.main(["off"], out=out) == 0 and calls[-1][0] == "remote_disable"


def test_the_console_command_reports_a_failure(monkeypatch):
    monkeypatch.setattr(netclient, "call", lambda *a, **k: {"ok": False, "message": "Remote access is only offered on the live medium."})
    import io
    out = io.StringIO()
    assert remote_cli.main(["on"], out=out) == 1 and "live medium" in out.getvalue()


def test_the_dialog_turns_remote_access_on_and_off(qtbot):
    state = {"on": False}

    def service(action, payload):
        if action == "remote_enable":
            state["on"] = True
            return {**STATUS_ON, "password": payload["password"] or "maple-river-stone42"}
        if action == "remote_disable":
            state["on"] = False
        return STATUS_ON if state["on"] else STATUS_OFF
    dialog = RemoteAccessDialog(service, synchronous=True)
    qtbot.addWidget(dialog)
    dialog.show()
    assert "OFF" in dialog.state.text() and dialog.enable_button.isVisibleTo(dialog) and not dialog.disable_button.isVisibleTo(dialog)
    dialog.enable_button.click()
    assert "ON" in dialog.state.text() and "ssh gentoo@192.168.1.20" in dialog.details.text() and "maple-river-stone42" in dialog.details.text()
    assert dialog.disable_button.isVisibleTo(dialog)
    dialog.disable_button.click()
    assert "OFF" in dialog.state.text() and "maple-river-stone42" not in dialog.details.text()


def test_the_dialog_without_a_service_says_so(qtbot):
    dialog = RemoteAccessDialog(None, synchronous=True)
    qtbot.addWidget(dialog)
    assert dialog.state.text() == netclient.UNAVAILABLE


# ------------------------------------------------------------------ netclient


def test_call_returns_the_result_data_or_a_failure(monkeypatch):
    from gentoo_installer import daemon_client
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: [{"type": "result", "data": {"ok": True, "x": 1}}, {"type": "completed", "data": {"success": True}}])
    assert netclient.call("/s", "x") == {"ok": True, "x": 1, "message": ""}
    monkeypatch.setattr(daemon_client, "request", lambda *a, **k: [{"type": "error", "error": "No.", "data": {"code": "bad_request"}}])
    assert netclient.call("/s", "x") == {"ok": False, "message": "No.", "code": "bad_request"}

    def broken(*a, **k):
        raise FileNotFoundError(2, "gone")
    monkeypatch.setattr(daemon_client, "request", broken)
    answer = netclient.call("/s", "x")
    assert answer["ok"] is False and answer["message"]


def test_calls_also_work_on_a_worker_thread(qtbot):
    widget = NetworkCard(InstallManifest(), caller=FakeService())
    qtbot.addWidget(widget)
    widget.refresh()
    qtbot.waitUntil(lambda: widget.status is not None, timeout=5000)
    assert widget.adapters.text() == "Wi-Fi: wlp4s0 (not connected)"
    qtbot.waitUntil(lambda: widget.network_list.count() == 3, timeout=5000)
    widget.wait()


# ------------------------------------------------------------------ review fixes (F1, F2, F4)


def test_the_router_dns_preset_means_the_gateway_address(card):
    manifest = InstallManifest()
    service = FakeService(network_status=STATUS_OK)
    widget = card(service, manifest)
    widget.refresh()
    fill_static(widget)
    assert widget.dns_preset.currentText().startswith("Router") and widget.dns_servers() == ["192.168.1.1"]
    widget.apply_static()
    assert service.payloads("network_static")[0]["dns"] == ["192.168.1.1"]
    network = manifest.network
    assert network.live_static.dns == ["192.168.1.1"] and network.dns == ["192.168.1.1"]
    assert manifest.validate_sections()["network"] == []


def test_an_own_dns_list_cannot_be_empty(card):
    widget = card()
    fill_static(widget)
    widget.dns_preset.setCurrentIndex(widget.dns_preset.count() - 1)
    widget.dns_own.setText("")
    assert "DNS" in widget.static_error()


def test_a_loaded_router_dns_shows_the_router_preset(card):
    manifest = InstallManifest()
    manifest.network.live_static.address, manifest.network.live_static.gateway = "10.0.0.5/24", "10.0.0.1"
    manifest.network.live_static.dns = ["10.0.0.1"]
    widget = card(manifest=manifest)
    widget.load_manifest()
    assert widget.dns_preset.currentText().startswith("Router")


def test_the_page_hides_the_problem_and_the_wifi_box_when_the_mirror_is_reachable(qtbot):
    page = page_with(qtbot, service=FakeService())  # the card's own tests say offline (ping or DNS fail on a proxy-only network)
    card = page.network_card
    card.refresh()
    assert card.problem.isVisibleTo(card) and card.wifi_box.isVisibleTo(card)
    info = sample_info()
    info.network = True
    page._network(info)
    assert card.problem.isHidden() and card.wifi_box.isHidden() and card.why_button.isHidden()


def test_wireless_tools_on_another_manager_say_wifi_will_not_connect_by_itself(make_window):
    window, page = customize(make_window)
    page.wireless_tools.setChecked(True)
    page._manager_picked()
    manager = page.network_manager.currentData()
    if manager == "networkmanager":
        page.network_manager.setCurrentIndex(next(i for i in range(page.network_manager.count()) if page.network_manager.itemData(i) == "dhcpcd"))
        page._manager_picked()
    assert "will not connect by itself" in page.network_note.text()
    assert "will not join" in network_tab.WIRELESS_HELP
