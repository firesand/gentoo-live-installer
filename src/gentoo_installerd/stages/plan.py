"""BOOT: the hardware plan of an install (planned once, saved, restored on resume)."""

from __future__ import annotations

import json
from pathlib import Path

from ..hwplan import READABLE_PLAN_SCHEMAS, HardwarePlan, plan as plan_hardware
from .context import _timestamp, _write_private, InstallError, StageContext


class PlanMixin(StageContext):
    # --- the hardware plan -----------------------------------------------------------------

    def _plan(self) -> HardwarePlan | None:
        """What this machine needs on top of the manifest (microcode, firmware, drivers, power, ...).

        Decided once per job from the hardware probe of the inspection, or restored on a resume so
        that the stages after the interruption follow the decisions of the stages before it. Every
        installation has a plan: without probe data it is the generic one, which still decides
        microcode and firmware from the manifest options.
        """
        if self.hardware_plan is None and not self._plan_attempted:
            self._plan_attempted = True
            self.hardware_plan = self._restore_plan()
            if self.hardware_plan is None:
                self.hardware_plan = self._make_plan()
                if self.hardware_plan is not None:
                    self._save_plan(self.hardware_plan)
        return self.hardware_plan

    def _make_plan(self) -> HardwarePlan | None:
        hardware = getattr(self.inspection, "hardware", None) or None
        try:
            return plan_hardware(self.manifest, hardware)
        except Exception as exc:  # noqa: BLE001 - a planning bug must not stop an install that can go on without it
            self.log(f"WARNING: the hardware-specific setup could not be planned ({exc}); continuing with the generic setup")
        if hardware is not None:
            try:
                return plan_hardware(self.manifest, None)
            except Exception:  # noqa: BLE001
                pass
        self.degraded.append("The hardware-specific setup (microcode, firmware, drivers) could not be planned and was skipped")
        return None

    @property
    def plan_path(self) -> Path:
        return self.state_dir / "hardware-plan.json"

    def _save_plan(self, plan: HardwarePlan) -> None:
        """Keep the decision (private, bound to this manifest) so a resume does not decide again.

        The file is on the live system only and holds the plan's file contents; the copy that goes
        to the installed system keeps just their paths and hashes.
        """
        if self.dry_run:
            return
        record = {"version": 1, "fingerprint": self.checkpoints.fingerprint, "created": _timestamp(), "plan": plan.to_dict()}
        try:
            _write_private(self.plan_path, json.dumps(record, indent=2) + "\n")
        except OSError as exc:
            self.log(f"WARNING: the hardware-specific setup could not be saved ({exc}); a resume will decide it again")

    def _restore_plan(self) -> HardwarePlan | None:
        """The plan an earlier run of this job decided, or None when this is not a resume or none was saved."""
        if not self.resume or self.dry_run:
            return None
        try:
            raw = json.loads(self.plan_path.read_text())
        except FileNotFoundError:
            self.log("No saved hardware-specific setup was found; deciding it again")
            return None
        except (OSError, ValueError) as exc:
            raise InstallError(f"The saved hardware-specific setup cannot be read ({exc}); start the installation again") from exc
        plan = raw.get("plan") if isinstance(raw, dict) else None
        if not isinstance(plan, dict) or raw.get("version") != 1 or raw.get("fingerprint") != self.checkpoints.fingerprint or plan.get("schema") not in READABLE_PLAN_SCHEMAS:
            raise InstallError("The saved hardware-specific setup does not belong to this installation; start the installation again")
        self.log("Using the hardware-specific setup that was decided when this installation started")
        return HardwarePlan.from_dict(plan)
