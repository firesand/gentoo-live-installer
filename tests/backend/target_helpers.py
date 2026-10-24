"""Shared helpers for the target-configuration tests (not collected as tests)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gentoo_installer.manifest import InstallManifest
from gentoo_installerd.configure import Configurer, Resources
from gentoo_installerd.runner import CommandResult, RunnerError, validate_argv

from tests.backend.helpers import manifest as base_manifest


class ScriptedRunner:
    """A runner that records everything and replays scripted outcomes per command prefix.

    ``script("chroot /t emerge", ("fail", "output"), "ok")`` makes the first matching command fail
    with that output and the following ones succeed ("*word" matches commands containing word).
    It accepts ``root_identity`` like the daemon's runner.
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self.inputs: list[str | None] = []
        self.root_identity: list[bool] = []
        self.options: list[dict[str, object]] = []
        self.outputs: dict[str, str] = {}
        self._scripts: list[tuple[str, list]] = []

    def script(self, prefix: str, *outcomes: object) -> None:
        self._scripts.append((prefix, list(outcomes)))

    def respond(self, prefix: str, stdout: str) -> None:
        self.outputs[prefix] = stdout

    def run(self, argv: Sequence[str], *, stdin: str | None = None, secret: bool = False,
            env: Mapping[str, str] | None = None, cwd: Path | None = None, root_identity: bool = False,
            timeout: float | None = None, capture: bool = True) -> CommandResult:
        args = validate_argv(argv)
        self.commands.append(args)
        self.envs.append(env)
        self.inputs.append(stdin)
        self.root_identity.append(root_identity)
        self.options.append({"timeout": timeout, "capture": capture, "root_identity": root_identity})
        line = " ".join(args)
        for prefix, outcomes in self._scripts:
            # a leading "*" means "contains" instead of "starts with"
            matches = prefix[1:] in line if prefix.startswith("*") else line.startswith(prefix)
            if matches and outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, tuple) and outcome[0] == "fail":
                    raise RunnerError(args, outcome[2] if len(outcome) > 2 else 1, outcome[1])
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, str) and outcome != "ok":
                    return CommandResult(args, 0, outcome)
                return CommandResult(args, 0, "")
        for prefix, stdout in self.outputs.items():
            if (prefix[1:] in line) if prefix.startswith("*") else line.startswith(prefix):
                return CommandResult(args, 0, stdout)
        return CommandResult(args, 0, "")

    def lines(self) -> list[str]:
        return [" ".join(command) for command in self.commands]


@dataclass
class FakePlan:
    """Stand-in for gentoo_installerd.hwplan.HardwarePlan (the fields configure.py reads)."""

    early_atoms: list[str] = field(default_factory=list)
    driver_atoms: list[str] = field(default_factory=list)
    required_atoms: list[str] = field(default_factory=list)
    optional_atoms: list = field(default_factory=list)
    package_use: dict[str, list[str]] = field(default_factory=dict)
    package_license: dict[str, list[str]] = field(default_factory=dict)
    package_mask: list[str] = field(default_factory=list)
    make_conf: dict[str, str] = field(default_factory=dict)
    files: list[tuple[str, str, int]] = field(default_factory=list)
    services: list[tuple[str, str]] = field(default_factory=list)
    kernel_cmdline: list[str] = field(default_factory=list)
    post_steps: list[list[str]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    first_steps: list[str] = field(default_factory=list)
    scalars: dict[str, object] = field(default_factory=dict)


def target_manifest(**sections: Mapping[str, object]) -> InstallManifest:
    """The test manifest (companions off) with ``section={"field": value}`` overrides."""
    m = base_manifest()
    for section, values in sections.items():
        for name, value in values.items():
            setattr(getattr(m, section), name, value)
    return m


def configurer(root: Path | str, runner: object | None = None, *, dry_run: bool = False, **kwargs: object) -> Configurer:
    """A Configurer with deterministic machine resources and no real sleeping."""
    result = Configurer(runner or ScriptedRunner(), root, dry_run=dry_run, **kwargs)  # type: ignore[arg-type]
    result.resources = lambda: Resources(8, 16 * 1024**3, 12 * 1024**3)
    result.sleep = lambda seconds: None
    return result


def mark_installed(root: Path, *packages: str) -> None:
    """Create vdb entries so Configurer.is_installed() sees category/name-1.0 as installed."""
    for atom in packages:
        category, _, name = atom.partition("/")
        (root / "var/db/pkg" / category / f"{name}-1.0").mkdir(parents=True, exist_ok=True)


def read(root: Path, relative: str) -> str:
    return (root / relative).read_text()


Callback = Callable[..., object]
