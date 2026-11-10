"""The unified state machine: preflight -> boot -> burn -> verify -> recover -> receipt.

Steps are derived from the profile, never hard-coded per project:

    preflight
    [boot:<chip>]        only when the chip needs a boot strap
    burn:<chip>
    [recover:<chip>]     before verify when ``recover.before_verify`` is set
    verify:<chip>
    [recover:<chip>]     after verify otherwise
    receipt

Execution is strictly sequential: a step may run only after the previous
step in this plan passed. A failed step is never marked done, so a rerun
cannot silently skip the write it needs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contract import SCHEMAS, now_bj, write_json

PREFLIGHT = "preflight"
RECEIPT = "receipt"


class StateError(RuntimeError):
    """Step is unknown, out of order, or the state file is corrupt."""


def plan_steps(profile) -> list[str]:
    steps = [PREFLIGHT]
    for chip in profile.chips:
        if chip.boot_required:
            steps.append(f"boot:{chip.id}")
        steps.append(f"burn:{chip.id}")
        if chip.recover_required and chip.recover_before_verify:
            steps.append(f"recover:{chip.id}")
        steps.append(f"verify:{chip.id}")
        if chip.recover_required and not chip.recover_before_verify:
            steps.append(f"recover:{chip.id}")
    steps.append(RECEIPT)
    return steps


def write_steps(plan: list[str]) -> set[str]:
    return {step for step in plan if step.startswith(("boot:", "burn:", "recover:"))}


class RunState:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "state.json"
        self.data: dict[str, Any] = {
            "schema": SCHEMAS["state"], "created_at": now_bj(),
            "completed": [], "receipts": {}, "failed": [],
        }
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise StateError(f"state file is corrupt: {self.path}: {exc}") from exc
            if self.data.get("schema") != SCHEMAS["state"]:
                raise StateError(f"state schema mismatch in {self.path}: {self.data.get('schema')!r}")

    def done(self, step: str) -> bool:
        return step in self.data.get("completed", [])

    def require(self, step: str, plan: list[str]) -> None:
        if step not in plan:
            raise StateError(f"step {step!r} is not part of this profile's plan {plan}")
        index = plan.index(step)
        if index == 0:
            return
        previous = plan[index - 1]
        if not self.done(previous):
            raise StateError(f"step {step} requires {previous} first (completed: {self.data.get('completed')})")

    def complete(self, step: str, verdict: str, receipt: Path | None = None) -> None:
        if verdict != "PASS":
            return
        completed = self.data.setdefault("completed", [])
        if step not in completed:
            completed.append(step)
        if receipt is not None:
            self.data.setdefault("receipts", {})[step] = str(receipt)
        self.save()

    def fail(self, step: str, verdict: str, reason: str, receipt: Path | None = None) -> None:
        entry = {"step": step, "verdict": verdict, "reason": reason, "at": now_bj(),
                 "receipt": str(receipt) if receipt else None}
        failures = self.data.setdefault("failed", [])
        failures.append(entry)
        if receipt is not None:
            self.data.setdefault("receipts", {})[step] = str(receipt)
        self.save()

    def save(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self.data)
