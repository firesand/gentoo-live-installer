"""gentoo-rescue-agents: pinned, verified before use, never as root, never a real binary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from gentoo_rescue_ai import agents

CLAUDE = b"#!/bin/sh\necho fake claude\n" * 50
FAKE_URL = {"claude": "https://downloads.claude.ai/claude-code-releases/9.9.9/linux-x64/claude",
            "codex": "https://github.com/openai/codex/releases/download/rust-v9/codex-x86_64-unknown-linux-musl.tar.gz",
            "opencode": "https://github.com/anomalyco/opencode/releases/download/v9/opencode-linux-x64-baseline.tar.gz"}


def tarball(members: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


CODEX = tarball({"codex-x86_64-unknown-linux-musl": b"fake codex binary" * 100})
OPENCODE = tarball({"opencode": b"fake opencode" * 100})
FILES = {"claude": CLAUDE, "codex": CODEX, "opencode": OPENCODE}


@pytest.fixture
def pins_file(tmp_path: Path) -> str:
    data = {"_comment": "test"}
    for name, blob in FILES.items():
        data[name] = {"version": "9.9.9", "url": FAKE_URL[name], "kind": "binary" if name == "claude" else "tar.gz", "binary": name,
                      "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "source": "test"}
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(data))
    return str(path)


def fetcher(files: dict[str, bytes], log: list[str] | None = None):
    def fetch(url: str, dest, limit: int) -> None:
        if log is not None:
            log.append(url)
        blob = files[os.path.basename(url)]
        if len(blob) > limit:
            raise agents.AgentError("the download is larger than the pinned size, so it was stopped")
        dest.write(blob)
    return fetch


def served(**override: bytes) -> dict[str, bytes]:
    files = {os.path.basename(FAKE_URL[name]): blob for name, blob in FILES.items()}
    for name, blob in override.items():
        files[os.path.basename(FAKE_URL[name])] = blob
    return files


@pytest.fixture(autouse=True)
def not_root(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


def run(tmp_path: Path, pins_file: str, *argv: str, files: dict[str, bytes] | None = None, log: list[str] | None = None) -> int:
    return agents.main(list(argv), fetch=fetcher(files if files is not None else served(), log), home=tmp_path / "home", pins_path=pins_file)


def test_the_shipped_pins_are_complete_https_and_checksummed() -> None:
    pins = agents.load_pins()
    assert set(pins) == {"claude", "codex", "opencode"}
    for pin in pins.values():
        assert pin.url.startswith("https://") and len(pin.sha256) == 64 and int(pin.sha256, 16) and pin.size > 1_000_000
        assert pin.version in pin.url
    assert pins["claude"].kind == "binary" and pins["codex"].kind == pins["opencode"].kind == "tar.gz"
    assert "bazel" not in pins["codex"].url


def test_install_downloads_verifies_and_installs_into_local_bin(tmp_path, pins_file, capsys) -> None:
    urls: list[str] = []
    assert run(tmp_path, pins_file, "install", "all", log=urls) == 0
    binaries = tmp_path / "home" / ".local" / "bin"
    assert sorted(path.name for path in binaries.iterdir()) == ["claude", "codex", "opencode"]
    assert (binaries / "claude").read_bytes() == CLAUDE
    assert (binaries / "codex").read_bytes() == b"fake codex binary" * 100  # the one program of the tarball, renamed
    assert all(os.access(binaries / name, os.X_OK) for name in FILES)
    assert urls == [FAKE_URL["claude"], FAKE_URL["codex"], FAKE_URL["opencode"]]
    assert "proprietary" in capsys.readouterr().out
    assert not [p for p in (tmp_path / "home" / ".local").iterdir() if p.name.startswith(".agent-")]  # scratch removed


def test_a_wrong_checksum_is_refused_and_nothing_is_installed(tmp_path, pins_file, capsys) -> None:
    evil = bytearray(CLAUDE)
    evil[0] ^= 1
    assert run(tmp_path, pins_file, "install", "claude", files=served(claude=bytes(evil))) == 1
    err = capsys.readouterr().err
    assert "does not match" in err and "nothing was installed" in err
    assert not (tmp_path / "home" / ".local" / "bin" / "claude").exists()
    assert agents.installed_version("claude", tmp_path / "home") is None


def test_a_wrong_size_is_refused_before_anything_is_unpacked(tmp_path, pins_file, capsys) -> None:
    assert run(tmp_path, pins_file, "install", "claude", files=served(claude=CLAUDE[:-1])) == 1
    assert "bytes" in capsys.readouterr().err
    assert not list((tmp_path / "home" / ".local" / "bin").glob("*"))


def test_a_bad_archive_with_the_right_checksum_installs_nothing(tmp_path, pins_file, monkeypatch) -> None:
    # A tarball with two programs, or only a symlink, or a path-traversal name: the pin matches, the content is still refused.
    for bad in (tarball({"a/codex": b"x" * 10, "b/codex": b"y" * 10}), tarball({}, {"codex": "/etc/passwd"}), tarball({"../../escape": b"x" * 10})):
        data = json.loads(Path(pins_file).read_text())
        data["codex"].update(sha256=hashlib.sha256(bad).hexdigest(), size=len(bad))
        Path(pins_file).write_text(json.dumps(data))
        assert run(tmp_path, pins_file, "install", "codex", files=served(codex=bad)) == 1
    assert not (tmp_path / "home" / ".local" / "bin" / "codex").exists() and not (tmp_path / "escape").exists()


def test_a_download_larger_than_the_pin_is_stopped(tmp_path, pins_file, capsys) -> None:
    assert run(tmp_path, pins_file, "install", "claude", files=served(claude=CLAUDE * 3)) == 1
    assert "larger than the pinned size" in capsys.readouterr().err


def test_from_dir_uses_the_same_checks(tmp_path, pins_file, capsys) -> None:
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / "claude").write_bytes(CLAUDE)
    assert run(tmp_path, pins_file, "install", "claude", "--from", str(usb), files={}) == 0  # the fetcher would KeyError: nothing was downloaded
    assert (tmp_path / "home" / ".local" / "bin" / "claude").read_bytes() == CLAUDE
    (usb / "claude").write_bytes(b"tampered" * 100)
    assert run(tmp_path, pins_file, "install", "claude", "--from", str(usb), files={}) == 1
    assert "refused" in capsys.readouterr().err
    assert (tmp_path / "home" / ".local" / "bin" / "claude").read_bytes() == CLAUDE  # the earlier good install is untouched
    assert run(tmp_path, pins_file, "install", "codex", "--from", str(usb), files={}) == 1
    err = capsys.readouterr().err
    assert "is not in" in err and FAKE_URL["codex"] in err


def test_status_and_remove(tmp_path, pins_file, capsys) -> None:
    assert run(tmp_path, pins_file, "status") == 0
    assert "claude    not installed (pinned: 9.9.9)" in capsys.readouterr().out
    run(tmp_path, pins_file, "install", "opencode")
    capsys.readouterr()
    run(tmp_path, pins_file, "status")
    assert "opencode  installed 9.9.9" in capsys.readouterr().out
    run(tmp_path, pins_file, "remove", "opencode")
    assert "removed" in capsys.readouterr().out
    assert agents.installed_version("opencode", tmp_path / "home") is None and not (tmp_path / "home" / ".local" / "bin" / "opencode").exists()


def test_it_never_runs_as_root_and_asks_which_agent(tmp_path, pins_file, monkeypatch, capsys) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert run(tmp_path, pins_file, "install", "claude") == 1
    assert "never run as root" in capsys.readouterr().err
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert run(tmp_path, pins_file, "install") == 2
    assert not (tmp_path / "home" / ".local" / "bin").exists()


def test_a_failed_download_names_the_offline_way(tmp_path, pins_file, capsys) -> None:
    def broken(url, dest, limit):
        raise OSError("Network is unreachable")

    assert agents.main(["install", "claude"], fetch=broken, home=tmp_path / "home", pins_path=pins_file) == 1
    err = capsys.readouterr().err
    assert "Network is unreachable" in err and "--from DIR" in err


def test_a_pin_file_with_a_foreign_host_or_http_is_refused(tmp_path) -> None:
    for url in ("http://downloads.claude.ai/x", "https://evil.example/x"):
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"claude": {"version": "1", "url": url, "kind": "binary", "binary": "claude", "sha256": "a" * 64, "size": 5}}))
        with pytest.raises(agents.AgentError):
            agents.load_pins(path)
