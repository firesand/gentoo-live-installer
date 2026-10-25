"""Wave 6 (harness): typing on the guest's keyboard, the port forward, and the AI rescue boot-menu alias. No VM is started."""
from __future__ import annotations

import pytest

import vm_iso
from scripts import vm_qmp

GRUB = """
menuentry 'Gentoo installer (desktop)' {
    linux /boot/gentoo root=live:CDLABEL=GENTOO quiet
    initrd /boot/gentoo.igz
}
menuentry 'Repair a system (text console)' {
    linux /boot/gentoo root=live:CDLABEL=GENTOO nox gentoo.autostart=rescue
    initrd /boot/gentoo.igz
}
menuentry 'AI rescue (text console)' {
    linux /boot/gentoo root=live:CDLABEL=GENTOO nox gentoo.autostart=ai-rescue
    initrd /boot/gentoo.igz
}
"""


def keys_sent(calls):
    """(qcode, shift) per key press, from the recorded input-send-event calls."""
    pressed = []
    for _path, command, arguments in calls:
        assert command == "input-send-event"
        events = arguments["events"]
        shift = any(event["data"]["key"]["data"] == "shift" for event in events)
        main = [event["data"]["key"]["data"] for event in events if event["data"]["key"]["data"] != "shift" and event["data"]["down"]]
        pressed.append((main[0], shift))
    return pressed


def type_with_fakes(text, **options):
    calls = []
    count = vm_qmp.type_text("qmp.sock", text, send=lambda path, command, arguments=None: calls.append((path, command, arguments)) or {}, sleep=lambda _s: None, **options)
    return count, calls


def test_lowercase_digits_and_dashes_are_typed_then_enter():
    count, calls = type_with_fakes("ab-9")
    assert count == 5 and keys_sent(calls) == [("a", False), ("b", False), ("minus", False), ("9", False), ("ret", False)]


def test_capitals_and_symbols_use_shift_and_release_it():
    _count, calls = type_with_fakes("A_", enter=False)
    assert keys_sent(calls) == [("a", True), ("minus", True)]
    events = calls[0][2]["events"]
    assert [(event["data"]["key"]["data"], event["data"]["down"]) for event in events] == [("shift", True), ("a", True), ("a", False), ("shift", False)]


def test_an_empty_text_is_just_enter():
    count, calls = type_with_fakes("")
    assert count == 1 and keys_sent(calls) == [("ret", False)]


def test_an_untypeable_character_is_refused_before_any_key_goes_out():
    calls = []
    with pytest.raises(ValueError, match="cannot type"):
        vm_qmp.type_text("qmp.sock", "good-é", send=lambda *args, **kwargs: calls.append(args), sleep=lambda _s: None)
    assert calls == [], "half a secret must never be typed"
    assert vm_qmp.untypeable("aé~é") == ["é", "~"]


def test_the_port_forward_is_asked_of_the_monitor_and_an_error_text_fails():
    calls = []
    vm_qmp.hostfwd_add("qmp.sock", 40123, 23456, send=lambda path, command, arguments=None: calls.append((command, arguments)) or {"return": ""})
    assert calls == [("human-monitor-command", {"command-line": "hostfwd_add net0 tcp:127.0.0.1:40123-10.0.2.15:23456"})]
    with pytest.raises(RuntimeError, match="refused the port forward"):
        vm_qmp.hostfwd_add("qmp.sock", 1, 2, send=lambda *args, **kwargs: {"return": "invalid host forwarding rule"})
    removed = []
    vm_qmp.hostfwd_remove("qmp.sock", 40123, send=lambda path, command, arguments=None: removed.append(arguments) or {})
    assert removed == [{"command-line": "hostfwd_remove net0 tcp:127.0.0.1:40123"}]


def test_the_ai_rescue_alias_picks_that_entry_and_leaves_the_repair_entry_alone():
    entries = vm_iso.parse_boot_menu(GRUB)
    assert vm_iso.select_entry(entries, "ai-rescue").title == "AI rescue (text console)"
    assert "gentoo.autostart=ai-rescue" in vm_iso.select_entry(entries, "ai-rescue").arguments
    assert vm_iso.select_entry(entries, "rescue").title == "Repair a system (text console)"
