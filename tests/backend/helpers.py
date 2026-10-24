from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from gentoo_installer.manifest import InstallManifest
from gentoo_installerd.inspect import Inspection
from gentoo_installerd.runner import CommandResult, RunnerError, validate_argv


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.secrets: list[bool] = []
        self.logs: list[str] = []
        self.responses: list[tuple[str, str]] = []
        self.failures: dict[str, int] = {}

    def respond(self, prefix: str, stdout: str) -> None:
        self.responses.append((prefix, stdout))

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> CommandResult:
        args = validate_argv(argv)
        self.commands.append(args)
        self.inputs.append(stdin)
        self.secrets.append(secret)
        for prefix, code in self.failures.items():
            if " ".join(args).startswith(prefix):
                raise RunnerError(args, code, "injected failure")
        output = ""
        for prefix, stdout in self.responses:
            if " ".join(args).startswith(prefix):
                output = stdout
                break
        return CommandResult(args, 0, output)


class FakeInspector:
    def __init__(self, errors: list[str] | None = None) -> None:
        self.errors = errors or []

    def inspect(self) -> Inspection:
        return Inspection("x86_64", 8 * 1024**3, True, True, {"lsblk": True}, [], list(self.errors))


def manifest() -> InstallManifest:
    result = InstallManifest()
    result.storage.device = "/dev/sda"
    result.storage.device_id = "disk-serial"
    result.accounts.password = "user-password"
    result.accounts.root_password = "root-password"
    result.options.install_hwmonux = False
    result.options.install_portagehub = False
    return result


def lsblk(*, serial: str = "disk-serial", mountpoint: str | None = None) -> str:
    import json
    return json.dumps({"blockdevices": [{"name": "sda", "path": "/dev/sda", "type": "disk", "size": 100000000000, "serial": serial, "wwn": "wwn-disk", "mountpoints": [], "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 1000000, "mountpoints": [mountpoint] if mountpoint else []}]}]})
