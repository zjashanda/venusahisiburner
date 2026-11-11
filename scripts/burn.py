#!/usr/bin/env python3
"""venusahisiburner - one burn skill for VenusA/CSK and HISI/HI3863.

Commands:
  plan        print the profile plan; no hardware access
  preflight   enumerate ports, read current versions, verify asset hashes
  step <id>   run exactly one step of the profile plan
  all         run the whole state machine and write run.receipt.json
  receipt     rebuild run.receipt.json from the completed steps

Guards (exit code 3): no profile, draft profile on a write step, no
``--confirm-write``, bench locked by another live process, unimplemented
driver. Phase FAIL/BLOCKED is exit code 4. Usage/profile/asset errors are 2,
including an unreadable or unresolvable ``--device-snapshot`` document.

Exit codes: 0 ok, 2 usage/profile/asset/snapshot, 3 refused (guard),
4 step FAIL/BLOCKED.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.assets import AssetError, Assets, FetchBlocked  # noqa: E402
from core.bench import Audit, BenchError, BenchLock  # noqa: E402
from core.contract import SCHEMAS, now_bj  # noqa: E402
from core.device_snapshot import (DeviceSnapshotError, apply_snapshot,  # noqa: E402
                                  resolve_snapshot)
from core.phases import (phase_boot, phase_burn, phase_preflight, phase_receipt,  # noqa: E402
                         phase_recover, phase_verify, safe_recover, _step_receipt)
from core.profile import Profile, ProfileError  # noqa: E402
from core.state import RECEIPT, RunState, StateError, plan_steps, write_steps  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_GUARD, EXIT_FAIL = 0, 2, 3, 4
REPO_ROOT = Path(__file__).resolve().parents[1]


def snapshot_lines(snapshot: dict) -> list[str]:
    lines = [
        "",
        f"device snapshot: {snapshot['source']}",
        f"  source: {snapshot['source']}",
        f"  schema: {snapshot['schema']}",
        f"  generated_at: {snapshot['generated_at'] or '-'}",
        f"  mode: {snapshot['mode']}",
        "  roles:",
    ]
    for role, item in snapshot["roles"].items():
        lines.append(f"    {role}: {item['port']} ({item['state']})")
    return lines


def plan_text(profile: Profile, assets: Assets, material_dir: Path | None, cache_dir: Path | None,
              snapshot: dict | None = None) -> str:
    info = profile.describe()
    lines = [
        f"node: {info['node']}",
        f"profile: {info['name']} ({info['status']}, family={info['family']}) at {info['profile']}",
        f"board: {info['board']}",
        f"time contract: {info['time_contract']['format']} ({info['time_contract']['zone']}, no offset)",
        f"control: {info['control']['kind']} {info['control']['port']}@{info['control']['baud']}",
        f"material dir: {material_dir or '-'}",
        f"asset cache: {cache_dir or '-'}",
        "",
        "steps:",
    ]
    for index, step in enumerate(plan_steps(profile), 1):
        detail = ""
        if step.startswith(("boot:", "burn:", "verify:", "recover:")):
            chip_id = step.split(":", 1)[1]
            chip = profile.chip(chip_id)
            if step.startswith("burn:"):
                detail = f"{chip.driver}: {chip.tool_asset} -> {chip.image_asset}"
            elif step.startswith("verify:"):
                detail = f"{chip.verify.get('version_key')}={chip.verify.get('expect')}"
            elif step.startswith("boot:"):
                detail = " / ".join(f"{key}={value}" for key, value in profile.commands.items()
                                    if key.startswith("strap") or key.startswith("power"))
            else:
                detail = str(chip.recover.get("command") or chip.recover.get("driver"))
        lines.append(f"  {index}. {step}" + (f"  [{detail}]" if detail else ""))
    lines += ["", "assets:"]
    for asset_id in profile.asset_ids():
        entry = assets.describe(asset_id)
        lines.append(f"  {asset_id}: {entry['target_chip']} {entry['version']} {entry['file']} "
                     f"sha256={entry['sha256'][:16]}... oss://<bucket>/{entry['oss']['key']}")
    if snapshot:
        lines += snapshot_lines(snapshot)
    return "\n".join(lines)


def _execute_step(ctx: dict, state: RunState, step: str) -> dict:
    profile: Profile = ctx["profile"]
    if ":" in step:
        kind, chip_id = step.split(":", 1)
        chip = profile.chip(chip_id)
        if kind == "boot":
            return phase_boot(ctx, chip)
        if kind == "burn":
            return phase_burn(ctx, chip)
        if kind == "verify":
            return phase_verify(ctx, chip)
        if kind == "recover":
            return phase_recover(ctx, chip)
        raise StateError(f"unknown step kind {kind!r}")
    if step == "preflight":
        return phase_preflight(ctx)
    if step == RECEIPT:
        return phase_receipt(ctx, state)
    raise StateError(f"unknown step {step!r}")


def _write_receipt_file(ctx: dict, state: RunState, receipt: dict) -> Path:
    path = Path(ctx["run_dir"]) / "run.receipt.json"
    payload = {"schema": SCHEMAS["receipt"], **receipt}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _guard_write(profile: Profile, steps: list[str], confirm_write: bool) -> None:
    writes = [step for step in steps if step in write_steps(plan_steps(profile))]
    if not writes:
        return
    if not profile.measured:
        missing = profile.raw.get("measurement_required") or []
        raise ProfileError(
            f"profile {profile.name} is draft; write steps {writes} refused until these are measured: {missing}")
    if not confirm_write:
        raise ProfileError(f"steps {writes} change power/flash state; re-run with --confirm-write")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - older consoles
        pass

    parser = argparse.ArgumentParser(description="VenusA/CSK + HISI/HI3863 unified burn driver")
    parser.add_argument("command", choices=["plan", "preflight", "step", "all", "receipt"])
    parser.add_argument("step_id", nargs="?", default=None, help="step id for 'step' (e.g. burn:venusa)")
    parser.add_argument("--profile", required=True, help="profile JSON path (no built-in default)")
    parser.add_argument("--run-dir", required=True, help="task run directory for receipts and logs")
    parser.add_argument("--assets", default=str(REPO_ROOT / "assets" / "manifest.json"),
                        help="unified asset manifest (default: <repo>/assets/manifest.json)")
    parser.add_argument("--material-dir", default=None, help="directory holding this run's material tree")
    parser.add_argument("--asset-cache", default=None, help="node-level tool cache (shared across projects)")
    parser.add_argument("--issue", default="", help="issue id recorded in receipts and reset_log")
    parser.add_argument("--expect", action="append", default=[], metavar="CHIP=VERSION",
                        help="override the expected version of one chip for this run")
    parser.add_argument("--fetch-assets", action="store_true",
                        help="download missing assets through the manifest's OSS method (env credentials only)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only, touch no hardware")
    parser.add_argument("--device-snapshot", default=None, metavar="FILE",
                        help="device-io snapshot or role-map JSON (ark.device-io.v1 / "
                             "ark.device-role-map.v1); its ports override the profile's")
    parser.add_argument("--confirm-write", action="store_true", help="required for boot/burn/recover steps")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    material_dir = Path(args.material_dir).expanduser().resolve() if args.material_dir else None
    cache_dir = Path(args.asset_cache).expanduser().resolve() if args.asset_cache else None

    try:
        profile = Profile(Path(args.profile).expanduser().resolve())
        snapshot: dict | None = None
        if args.device_snapshot:
            snapshot = resolve_snapshot(Path(args.device_snapshot).expanduser().resolve(), profile)
            apply_snapshot(profile, snapshot)
        assets = Assets(Path(args.assets).expanduser().resolve())
        assets.require(profile.asset_ids())
        plan = plan_steps(profile)

        if args.command == "plan" or args.dry_run:
            print(plan_text(profile, assets, material_dir, cache_dir, snapshot))
            print("\n[dry-run] no serial port opened, no burn tool started")
            return EXIT_OK

        expect: dict[str, str] = {}
        for item in args.expect:
            if "=" not in item:
                raise ProfileError(f"--expect needs CHIP=VERSION, got {item!r}")
            chip_id, version = item.split("=", 1)
            profile.chip(chip_id.strip())
            expect[chip_id.strip()] = version.strip()

        if args.fetch_assets:
            for asset_id in profile.asset_ids():
                report = assets.verify(asset_id, material_dir, cache_dir)
                if report["sha_ok"] and report["size_ok"]:
                    continue
                if not cache_dir:
                    print(f"asset {asset_id} needs --asset-cache to land the download", file=sys.stderr)
                    continue
                install = assets.get(asset_id)["install"]
                dest = cache_dir / str(install.get("cache_rel") or assets.get(asset_id)["file"])
                try:
                    print(json.dumps(assets.fetch(asset_id, dest), ensure_ascii=False))
                except FetchBlocked as exc:
                    print(f"fetch blocked for {asset_id}: {exc}", file=sys.stderr)

        ctx: dict[str, Any] = {
            "profile": profile, "run_dir": run_dir, "material_dir": material_dir,
            "cache_dir": cache_dir, "assets": assets, "expect": expect,
            "issue": args.issue or profile.node,
            "audit": Audit(run_dir, args.issue or profile.node),
            "device_snapshot": snapshot,
        }
        state = RunState(run_dir)

        if args.command == "step":
            if not args.step_id:
                raise ProfileError("command 'step' needs a step id, e.g. step burn:venusa")
            steps = [args.step_id]
        elif args.command == "receipt":
            steps = [RECEIPT]
        elif args.command == "preflight":
            steps = ["preflight"]
        else:
            steps = list(plan)

        _guard_write(profile, steps, args.confirm_write)
        state.require(steps[0], plan)

        worst = EXIT_OK
        receipt_path: Path | None = None
        with BenchLock(profile.node, Path(profile.raw["lock_path"]) if profile.raw.get("lock_path") else None):
            for step in steps:
                state.require(step, plan)
                receipt = _execute_step(ctx, state, step)
                verdict = str(receipt.get("verdict", "FAIL"))
                if step == RECEIPT:
                    receipt_path = _write_receipt_file(ctx, state, receipt)
                    state.complete(step, verdict, receipt_path)
                    print(json.dumps({"step": step, "verdict": verdict, "receipt": str(receipt_path)},
                                     ensure_ascii=False))
                    if verdict != "PASS":
                        worst = EXIT_FAIL
                    break
                path = _step_receipt(ctx, step, receipt)
                state.complete(step, verdict, path)
                if verdict != "PASS":
                    state.fail(step, verdict, str(receipt.get("reason", "")), path)
                    worst = EXIT_FAIL
                print(json.dumps({"step": step, "verdict": verdict, "receipt": str(path)}, ensure_ascii=False))
                if worst != EXIT_OK:
                    if args.command == "all":
                        recovery = safe_recover(ctx, f"{step} -> {verdict}")
                        print(json.dumps({"step": "safe-recover", "result": recovery}, ensure_ascii=False))
                        failed_receipt = phase_receipt(ctx, state)
                        receipt_path = _write_receipt_file(ctx, state, failed_receipt)
                        print(json.dumps({"step": "receipt", "verdict": failed_receipt.get("verdict"),
                                          "receipt": str(receipt_path)}, ensure_ascii=False))
                    break
        return worst
    except ProfileError as exc:
        print(f"venusahisiburner: {exc}", file=sys.stderr)
        text = str(exc)
        guard = any(token in text for token in ("draft", "confirm-write"))
        return EXIT_GUARD if guard else EXIT_USAGE
    except DeviceSnapshotError as exc:
        print(f"venusahisiburner: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (BenchError, StateError) as exc:
        print(f"venusahisiburner: {exc}", file=sys.stderr)
        return EXIT_GUARD if "locked" in str(exc) or "不抢占" in str(exc) else EXIT_USAGE
    except (AssetError, FetchBlocked) as exc:
        print(f"venusahisiburner: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
