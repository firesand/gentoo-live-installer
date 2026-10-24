"""Wave 3 review fixes: a proxy password is hidden completely, also behind an e-mail user name or with an @ inside (PROXY-1)."""

from __future__ import annotations

import json

import pytest

from gentoo_installer import diagnostics
from gentoo_installer.app import blank_secrets
from gentoo_installer.manifest import InstallManifest
from gentoo_installer.textui.inventory import MachineView
from gentoo_installer.textui.summary import build_summary, render_summary, settings_rows

from tests.textui.support import valid_manifest

# (what is typed, what the manifest itself shows for it, the password that must not appear)
PROXIES = [
    ("http://john@corp.com:s3cretpw@proxy.corp.example:3128", "http://john@corp.com:<redacted>@proxy.corp.example:3128", "s3cretpw"),
    ("http://svc:p@ssw0rd@proxy.corp.example:3128", "http://svc:<redacted>@proxy.corp.example:3128", "ssw0rd"),
    ("http://alice:S3cretPass@proxy.corp:3128", "http://alice:<redacted>@proxy.corp:3128", "S3cretPass"),
]


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_the_manifest_accepts_these_proxies_and_hides_the_password(typed: str, shown: str, password: str) -> None:
    manifest = valid_manifest()
    manifest.network.proxy = typed
    assert manifest.validate() == []
    assert manifest.to_dict(redact=True)["network"]["proxy"] == shown


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_redact_url_userinfo_follows_the_manifests_rule(typed: str, shown: str, password: str) -> None:
    assert diagnostics.redact_url_userinfo(typed) == shown
    assert password not in diagnostics.redact_url_userinfo(typed)


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_the_saved_draft_keeps_no_part_of_the_credentials(typed: str, shown: str, password: str) -> None:
    manifest = valid_manifest()
    manifest.network.proxy = typed
    draft = blank_secrets(manifest.to_dict(redact=True))["network"]["proxy"]
    assert draft == "http://" + typed.rpartition("@")[2]  # only the host and port are kept
    assert password not in draft and "<redacted>" not in draft


def test_text_around_the_address_is_left_alone() -> None:
    assert diagnostics.redact_url_userinfo("see https://bob:pw@host/x and http://carol:pw2@h2") == "see https://bob:<redacted>@host/x and http://carol:<redacted>@h2"
    assert diagnostics.redact_url_userinfo("https://example.org/@someone and http://host?mail=a@b.c") == "https://example.org/@someone and http://host?mail=a@b.c"
    assert diagnostics.redact_url_userinfo("http://proxy.corp:3128") == "http://proxy.corp:3128"
    assert diagnostics.strip_url_userinfo("socks5h://u:p@h:1080") == "socks5h://h:1080"


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_the_summary_before_the_typed_confirmation_hides_the_password(typed: str, shown: str, password: str) -> None:
    manifest = valid_manifest()
    manifest.network.proxy = typed
    rows = dict(settings_rows(manifest, MachineView()))
    assert password not in rows["Network"] and "<redacted>" in rows["Network"]
    summary = build_summary(manifest, MachineView())
    assert password not in json.dumps(summary.to_dict())  # the settings rows of the --dry-run --json-events output


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_the_rendered_summary_hides_the_password(typed: str, shown: str, password: str) -> None:
    from gentoo_installer.textui.console import Console
    import io

    manifest = valid_manifest()
    manifest.network.proxy = typed
    out = io.StringIO()
    render_summary(Console(io.StringIO(), out, environ={}), build_summary(manifest, MachineView()))
    assert password not in out.getvalue()


@pytest.mark.parametrize(("typed", "shown", "password"), PROXIES)
def test_the_network_step_and_the_proxy_question_hide_the_password(typed: str, shown: str, password: str, host) -> None:  # type: ignore[no-untyped-def]
    from gentoo_installer.textui.console import Console
    from gentoo_installer.textui.daemon import Daemon
    from gentoo_installer.textui.wizard import Wizard

    from tests.textui.support import Script

    manifest = InstallManifest()
    manifest.network.proxy = typed
    script = Script([("Download proxy:", "p"), ("keep to leave", "keep"), ("Download proxy:", "2")])
    console = Console(script.stdin, script.out, environ={})
    wizard = Wizard(console, host, Daemon("/nonexistent.sock"), manifest=manifest)
    wizard.refresh_view = lambda: True  # type: ignore[method-assign]
    wizard.step_network()
    script.assert_finished()
    assert password not in script.text and "<redacted>" in script.text
