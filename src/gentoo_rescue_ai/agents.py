"""``gentoo-rescue-agents install|status|remove [claude|codex|opencode|all] [--from DIR]``.

The three agents are never part of the ISO. They are downloaded on first use, from their own makers, at the versions pinned in
``data/pins.json`` (with the expected size and sha256), and are checked BEFORE anything is unpacked or made executable: a
file that does not match is deleted and refused. They go to ``~/.local/bin`` of the user ``rescue-ai``. Nothing here runs as
root and nothing here ever runs an agent. ``--from DIR`` takes the same files from a USB stick (offline), with the same checks.

Claude Code is proprietary software: it is downloaded by the person using the stick, straight from Anthropic, and is never
redistributed. The helper does not touch Claude's login (see login.py).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import BinaryIO, Callable, Sequence
from urllib.parse import urlparse

AGENTS = ("claude", "codex", "opencode")
ALLOWED_HOSTS = ("downloads.claude.ai", "github.com", "releases.openai.com")
MIN_FREE_EXTRA = 64 * 1024 * 1024
MAX_EXTRACTED = 2 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024
TERMS = {
    "claude": "Claude Code is Anthropic's proprietary software. It is downloaded from Anthropic for you and is subject to Anthropic's terms: https://code.claude.com/docs/en/legal-and-compliance",
    "codex": "Codex CLI is open source (Apache-2.0) from OpenAI.",
    "opencode": "OpenCode is open source (MIT).",
}


class AgentError(Exception):
    """Something the person should read; the text says what to do."""


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    url: str
    kind: str  # "binary" or "tar.gz"
    binary: str
    sha256: str
    size: int

    @property
    def filename(self) -> str:
        return os.path.basename(urlparse(self.url).path)


Fetcher = Callable[[str, BinaryIO, int], None]  # url, destination file, maximum number of bytes


def load_pins(path: str | os.PathLike[str] | None = None) -> dict[str, Pin]:
    text = Path(path).read_text(encoding="utf-8") if path else resources.files("gentoo_rescue_ai").joinpath("data/pins.json").read_text(encoding="utf-8")
    pins: dict[str, Pin] = {}
    for name, item in json.loads(text).items():
        if name.startswith("_"):
            continue
        pin = Pin(name, item["version"], item["url"], item["kind"], item["binary"], item["sha256"], int(item["size"]))
        host = urlparse(pin.url).hostname or ""
        if urlparse(pin.url).scheme != "https" or host not in ALLOWED_HOSTS or len(pin.sha256) != 64 or pin.kind not in {"binary", "tar.gz"} or pin.size <= 0:
            raise AgentError(f"the pin for {name} is not valid")
        pins[name] = pin
    return pins


def bin_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return Path(home or Path.home()) / ".local" / "bin"


def state_file(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    return Path(home or Path.home()) / ".local" / "share" / "gentoo-rescue-agents" / f"{name}.json"


def download(url: str, dest: BinaryIO, limit: int) -> None:
    """Stream ``url`` into ``dest`` (https only, no credentials), stopping with an error when it is larger than ``limit``."""
    request = urllib.request.Request(url, headers={"User-Agent": "gentoo-rescue-agents"})
    done, shown = 0, 0
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - the URL is a pinned https URL (load_pins)
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                return
            done += len(chunk)
            if done > limit:
                raise AgentError("the download is larger than the pinned size, so it was stopped")
            dest.write(chunk)
            if done - shown >= 25 * CHUNK:
                shown = done
                print(f"  {done // CHUNK} MiB ...", file=sys.stderr, flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, pin: Pin) -> None:
    """Raise unless ``path`` has exactly the pinned size and sha256. Nothing else touches an unverified file."""
    size = path.stat().st_size
    if size != pin.size:
        raise AgentError(f"{pin.filename} has {size} bytes, but {pin.size} were expected: refused")
    if not hmac_equal(sha256_of(path), pin.sha256):
        raise AgentError(f"{pin.filename} does not match its pinned checksum: refused (nothing was installed)")


def hmac_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.lower(), b.lower())


def pick_member(archive: tarfile.TarFile, binary: str) -> tarfile.TarInfo:
    """The one regular file of the archive that is the program: named exactly ``binary``, or the only file whose name starts with it."""
    files = [m for m in archive.getmembers() if m.isreg() and m.size > 0]
    exact = [m for m in files if os.path.basename(m.name) == binary]
    close = [m for m in files if os.path.basename(m.name).startswith(binary)]
    chosen = exact or close
    if len(chosen) != 1:
        raise AgentError(f"the archive does not hold exactly one {binary} program")
    if chosen[0].size > MAX_EXTRACTED:
        raise AgentError("the program in the archive is unreasonably large")
    return chosen[0]


def place(pin: Pin, verified: Path, target_dir: Path) -> Path:
    """Put a VERIFIED download in ``target_dir`` as ``pin.binary``, atomically; a tarball gives up only its one program, by name, never by path."""
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / pin.binary
    fd, temp_name = tempfile.mkstemp(prefix=f".{pin.binary}.", dir=target_dir)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            if pin.kind == "binary":
                with verified.open("rb") as source:
                    shutil.copyfileobj(source, out, CHUNK)
            else:
                with tarfile.open(verified, "r:gz") as archive:
                    member = pick_member(archive, pin.binary)
                    packed = archive.extractfile(member)
                    if packed is None:
                        raise AgentError("the program cannot be read from the archive")
                    shutil.copyfileobj(packed, out, CHUNK)
        temp.chmod(0o755)
        os.replace(temp, final)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return final


def free_space_ok(directory: Path, pin: Pin) -> None:
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < pin.size * 2 + MIN_FREE_EXTRA:
        raise AgentError(f"not enough free space for {pin.name}: it needs about {(pin.size * 2 + MIN_FREE_EXTRA) // (1024 * 1024)} MiB (the stick keeps files in RAM; try a PC with more memory, or install one agent only)")


def install_one(pin: Pin, *, home: str | os.PathLike[str] | None = None, fetch: Fetcher = download, source_dir: str | os.PathLike[str] | None = None,
                say: Callable[[str], None] = print) -> Path:
    target_dir = bin_dir(home)
    free_space_ok(target_dir, pin)
    target_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".agent-", dir=target_dir.parent))
    try:
        download_path = scratch / pin.filename
        if source_dir is not None:
            found = Path(source_dir) / pin.filename
            if not found.is_file():
                raise AgentError(f"{pin.filename} is not in {source_dir}. Copy it there from: {pin.url}")
            say(f"Copying {pin.filename} from {source_dir} ...")
            shutil.copyfile(found, download_path)  # verify the copy that will be used, not the original that could change
        else:
            say(f"Downloading {pin.name} {pin.version} from {urlparse(pin.url).hostname} ...")
            with download_path.open("wb") as handle:
                try:
                    fetch(pin.url, handle, pin.size)
                except OSError as exc:
                    raise AgentError(f"the download failed ({exc}). Check the network (gentoo-rescue-ai offers Wi-Fi), or use --from DIR with a copy from: {pin.url}") from exc
        verify(download_path, pin)
        say("Checksum is correct.")
        final = place(pin, download_path, target_dir)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    record = state_file(pin.name, home)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"version": pin.version, "sha256": pin.sha256, "binary": str(final)}), encoding="utf-8")
    return final


def installed_version(name: str, home: str | os.PathLike[str] | None = None) -> str | None:
    try:
        record = json.loads(state_file(name, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pin_binary = Path(record.get("binary", ""))
    return str(record.get("version")) if pin_binary.is_file() else None


def remove_one(pin: Pin, home: str | os.PathLike[str] | None = None) -> bool:
    gone = False
    for path in (bin_dir(home) / pin.binary, state_file(pin.name, home)):
        if path.exists() or path.is_symlink():
            path.unlink()
            gone = True
    return gone


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gentoo-rescue-agents", description="Install the AI agents on first use, at pinned and checksum-verified versions.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("install", "download (or copy with --from) and verify the agents"), ("status", "what is installed"), ("remove", "delete an agent")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("agent", nargs="?", choices=[*AGENTS, "all"], default="all" if name == "status" else None)
        if name == "install":
            item.add_argument("--from", dest="source", metavar="DIR", help="take the files from this folder (for example a USB stick) instead of the network")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, *, fetch: Fetcher = download, home: str | os.PathLike[str] | None = None, pins_path: str | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.command != "status" and args.agent is None:
        print(f"gentoo-rescue-agents {args.command}: say which one: claude, codex, opencode or all", file=sys.stderr)
        return 2
    if args.command != "status" and hasattr(os, "geteuid") and os.geteuid() == 0:
        print("gentoo-rescue-agents: do not run this as root. Use the rescue-ai shell (the window named shell of the rescue session: Ctrl+B n): downloads never run as root.", file=sys.stderr)
        return 1
    try:
        pins = load_pins(pins_path)
        chosen = [pins[name] for name in AGENTS if args.agent in {name, "all"}]
        code = 0
        for pin in chosen:
            if args.command == "status":
                version = installed_version(pin.name, home)
                if version is None:
                    print(f"{pin.name:<9} not installed (pinned: {pin.version})")
                else:
                    print(f"{pin.name:<9} installed {version}" + ("" if version == pin.version else f" (the pin is {pin.version}; install again to update)"))
            elif args.command == "remove":
                print(f"{pin.name}: " + ("removed" if remove_one(pin, home) else "was not installed"))
            else:
                print(TERMS[pin.name])
                try:
                    path = install_one(pin, home=home, fetch=fetch, source_dir=args.source)
                    print(f"{pin.name} {pin.version} is installed as {path}. Log in with: gentoo-rescue-login {pin.name}")
                except AgentError as exc:
                    print(f"gentoo-rescue-agents: {pin.name}: {exc}", file=sys.stderr)
                    code = 1
        return code
    except AgentError as exc:
        print(f"gentoo-rescue-agents: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
