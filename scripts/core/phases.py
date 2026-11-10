"""Phase implementations: preflight, boot, burn, verify, recover, receipt.

The drivers are generic in shape and profile-driven in detail; only the two
verified tool families are implemented:

  ``venusa-uart``    Uart_Burn_Tool ... -b <baud> -p <port> -f <image> -a <addr>
  ``hisi-burntool``  BurnTool.exe -com:<n> -bin:<file> -signalbaud:<baud> -chiptype:<id>

``csk6-batch`` is declared in the profile schema but refused as a write
driver until a Media bench has been measured on this node.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .bench import BenchError, PortLogger, open_control, power_cycle, run_tool, send_control, serial_module
from .contract import (SCHEMAS, TIME_CONTRACT, classify_asr, now_bj, parse_versions,
                       read_text_any, sha256_file)
from .state import RECEIPT, plan_steps


def _step_receipt(ctx: dict, step: str, receipt: dict) -> Path:
    name = step.replace(":", "-")
    path = Path(ctx["run_dir"]) / "steps" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMAS["phase"], "step": step, "at": now_bj(), **receipt}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def enumerate_ports() -> list[dict[str, str]]:
    serial = serial_module()
    ports = []
    for info in sorted(serial.tools.list_ports.comports(), key=lambda item: item.device):
        ports.append({"device": info.device, "description": info.description, "hwid": info.hwid})
    return ports


def version_matches(expected: str, got: str) -> bool:
    if not expected or not got:
        return False
    expected, got = expected.strip(), got.strip()
    if "/" in expected:
        return expected.lower() == got.lower()
    return got.split("/")[-1].lower() == expected.lower()


def expectation_report(ctx: dict, chip, versions: dict[str, str]) -> dict[str, Any]:
    key = str(chip.verify.get("version_key"))
    override = (ctx.get("expect") or {}).get(chip.id)
    expected = override or str(chip.verify.get("expect") or "")
    got = versions.get(key, "")
    report: dict[str, Any] = {"chip": chip.id, "version_key": key, "expected": expected,
                              "got": got, "overridden": bool(override)}
    if expected:
        report["match"] = version_matches(expected, got)
    return report


# ---------------------------------------------------------------- preflight


def phase_preflight(ctx: dict) -> dict:
    profile = ctx["profile"]
    started = now_bj()
    if not profile.measured:
        return {"started_at": started, "verdict": "BLOCKED",
                "reason": "profile is draft: measure the bench first",
                "measurement_required": profile.raw.get("measurement_required") or []}

    ports = enumerate_ports()
    expected = profile.expected_ports()
    present = {item["device"] for item in ports}
    missing = sorted(set(expected) - present)

    captured: dict[str, Any] = {}
    all_lines: list[str] = []
    for chip in profile.chips:
        logger = PortLogger(chip.log_port, int(chip.log_baud),
                            Path(ctx["run_dir"]) / "serial_logs" / f"preflight_{chip.id}.log")
        logger.start()
        time.sleep(float(profile.raw.get("preflight_window_s", 6)))
        logger.stop_event.set()
        time.sleep(0.6)
        snapshot = logger.snapshot()
        snapshot["versions"] = parse_versions(logger.lines)
        captured[chip.id] = snapshot
        all_lines.extend(logger.lines)

    assets_report = ctx["assets"].verify_all(profile.asset_ids(), ctx.get("material_dir"), ctx.get("cache_dir"))
    versions = parse_versions(all_lines)
    expectations = {chip.id: expectation_report(ctx, chip, versions) for chip in profile.chips}

    if missing:
        verdict, reason = "FAIL", f"expected ports missing: {missing}"
    elif not assets_report["all_ok"]:
        bad = [asset_id for asset_id, item in assets_report["assets"].items() if not (item["sha_ok"] and item["size_ok"])]
        verdict, reason = "BLOCKED", f"asset files missing or hash mismatch: {bad}"
    else:
        verdict, reason = "PASS", ""
    return {
        "started_at": started, "ports": ports, "expected_ports": expected, "missing_ports": missing,
        "captured": captured, "versions_before": versions, "expectations": expectations,
        "assets": assets_report, "verdict": verdict, "reason": reason,
    }


# ---------------------------------------------------------------- boot


def phase_boot(ctx: dict, chip) -> dict:
    profile = ctx["profile"]
    started = now_bj()
    if str(profile.control.get("kind")) != "serial-relay":
        raise BenchError(f"boot strap needs control.kind=serial-relay, got {profile.control.get('kind')!r}")
    timing = profile.control.get("timing") or {}
    handle = open_control(profile)
    records: dict[str, Any] = {}
    try:
        records["power_cut"] = send_control(handle, profile, f"boot:{chip.id}",
                                            profile.commands["power_cut"], ctx["audit"])
        time.sleep(float(timing.get("cut_to_strap_s", 2.0)))
        records["strap_on"] = send_control(handle, profile, f"boot:{chip.id}",
                                           profile.commands["strap_on"], ctx["audit"])
        time.sleep(float(timing.get("strap_to_restore_s", 1.0)))
        records["power_restore"] = send_control(handle, profile, f"boot:{chip.id}",
                                                profile.commands["power_restore"], ctx["audit"])
    finally:
        handle.close()
    time.sleep(float(timing.get("restore_to_strap_off_s", 8.0)))
    handle = open_control(profile)
    try:
        records["strap_off"] = send_control(handle, profile, f"boot:{chip.id}",
                                            profile.commands["strap_off"], ctx["audit"])
    finally:
        handle.close()
    ok = all(record["receipt_ok"] for record in records.values())
    return {"started_at": started, "chip": chip.id, "control": records,
            "verdict": "PASS" if ok else "FAIL",
            "reason": "" if ok else "a control command did not return its receipt token"}


# ---------------------------------------------------------------- burn


def _burn_venusa(ctx: dict, chip) -> dict:
    profile = ctx["profile"]
    tool = ctx["assets"].resolve(chip.tool_asset, ctx.get("material_dir"), ctx.get("cache_dir"))
    image = ctx["assets"].resolve(chip.image_asset, ctx.get("material_dir"), ctx.get("cache_dir"))
    burn = chip.burn
    argv = [str(tool), "-b", str(chip.transport["burn_baud"]), "-p", str(chip.transport["burn_port"]),
            "-f", str(image)] + [str(item) for item in burn.get("tool_args") or []] + \
           ["-a", str(burn.get("image_addr"))]
    result = run_tool(argv, Path(tool).parent, float(burn.get("timeout_s", 600)))
    markers = {marker: marker in result["output"] for marker in burn.get("success_markers") or []}
    log_path = Path(ctx["run_dir"]) / "tool_logs" / f"tool_{chip.id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result["output"], encoding="utf-8", newline="\n")
    ok = result["rc"] == 0 and bool(markers) and all(markers.values())
    return {
        "chip": chip.id, "driver": chip.driver, "tool": str(tool), "tool_sha256": sha256_file(tool),
        "image": str(image), "image_sha256": sha256_file(image), "image_addr": burn.get("image_addr"),
        "burn_port": chip.transport["burn_port"], "burn_baud": chip.transport["burn_baud"],
        "tool_result": {key: result[key] for key in ("cmd", "rc", "elapsed_s")},
        "tool_log": str(log_path), "markers": markers,
        "verdict": "PASS" if ok else "FAIL",
        "reason": "" if ok else "tool exit/output markers did not confirm a successful flash",
    }


def _burn_hisi(ctx: dict, chip) -> dict:
    profile = ctx["profile"]
    tool = ctx["assets"].resolve(chip.tool_asset, ctx.get("material_dir"), ctx.get("cache_dir"))
    binary = ctx["assets"].resolve(chip.image_asset, ctx.get("material_dir"), ctx.get("cache_dir"))
    burn = chip.burn
    argv = [str(tool), f"-com:{chip.transport['burn_port']}", f"-bin:{binary}",
            f"-signalbaud:{burn.get('signal_baud')}", f"-chiptype:{burn.get('chip_type')}"]
    argv += [f"-onlyburn:{part}" for part in burn.get("onlyburn_parts") or []]
    out_dir = Path(ctx["run_dir"]) / "tool_logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    tool_log = out_dir / f"tool_{chip.id}.log"
    opt_dir = Path(tool).parent / "optLog"
    before = {item.name for item in opt_dir.glob("optLog_*.txt")} if opt_dir.is_dir() else set()

    with tool_log.open("wb") as sink:
        proc = subprocess.Popen(argv, cwd=str(Path(tool).parent), stdout=sink,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    time.sleep(1.5)
    handle = open_control(profile)
    try:
        cut = send_control(handle, profile, f"burn:{chip.id}", profile.commands["power_cut"], ctx["audit"])
        time.sleep(3.0)
        restore = send_control(handle, profile, f"burn:{chip.id}", profile.commands["power_restore"], ctx["audit"])
    finally:
        handle.close()

    success_tokens = burn.get("success_tokens") or []
    failure_tokens = burn.get("failure_tokens") or []
    started = time.monotonic()
    opt_text, opt_path, saw_success, saw_failure = "", "", False, False
    while time.monotonic() - started < float(burn.get("timeout_s", 420)):
        if proc.poll() is not None:
            break
        fresh = [item for item in opt_dir.glob("optLog_*.txt") if item.name not in before] if opt_dir.is_dir() else []
        if fresh:
            newest = max(fresh, key=lambda item: item.stat().st_mtime)
            text = read_text_any(newest)
            if any(token in text for token in success_tokens + failure_tokens):
                opt_path, opt_text = str(newest), text
                saw_success = any(token in text for token in success_tokens)
                saw_failure = any(token in text for token in failure_tokens)
                break
        time.sleep(2.0)
    tool_exit = proc.poll()
    if tool_exit is None:
        proc.kill()
        proc.wait()

    handle = open_control(profile)
    try:
        cut2 = send_control(handle, profile, f"burn:{chip.id}", profile.commands["power_cut"], ctx["audit"])
        time.sleep(3.0)
        restore2 = send_control(handle, profile, f"burn:{chip.id}", profile.commands["power_restore"], ctx["audit"])
    finally:
        handle.close()

    receipts_ok = all(item["receipt_ok"] for item in (cut, restore, cut2, restore2))
    ok = saw_success and not saw_failure and receipts_ok
    return {
        "chip": chip.id, "driver": chip.driver, "tool": str(tool), "tool_sha256": sha256_file(tool),
        "image": str(binary), "image_sha256": sha256_file(binary),
        "cmd": [Path(argv[0]).name] + argv[1:], "tool_log": str(tool_log),
        "optlog": opt_path, "optlog_tail": opt_text[-1200:], "tool_exit": tool_exit,
        "power": {"cut": cut, "restore": restore, "cut2": cut2, "restore2": restore2},
        "verdict": "PASS" if ok else "FAIL",
        "reason": "" if ok else "BurnTool optLog did not confirm success or a power receipt failed",
    }


def phase_burn(ctx: dict, chip) -> dict:
    if chip.driver == "venusa-uart":
        return _burn_venusa(ctx, chip)
    if chip.driver == "hisi-burntool":
        return _burn_hisi(ctx, chip)
    raise BenchError(f"burn driver {chip.driver!r} is declared but not implemented on this node")


# ---------------------------------------------------------------- verify


def _capture_window(ctx: dict, step: str, window_s: float) -> dict:
    profile = ctx["profile"]
    loggers = []
    for chip in profile.chips:
        if not chip.log_port:
            continue
        logger = PortLogger(chip.log_port, int(chip.log_baud),
                            Path(ctx["run_dir"]) / "serial_logs" / f"{step.replace(':', '-')}_{chip.id}.log")
        logger.start()
        loggers.append((chip.id, logger))
    time.sleep(2.0)
    handle = open_control(profile)
    try:
        power = power_cycle(handle, profile, step, ctx["audit"])
    finally:
        handle.close()
    time.sleep(window_s)
    for _, logger in loggers:
        logger.stop_event.set()
    time.sleep(0.8)
    captured = {chip_id: logger.snapshot() for chip_id, logger in loggers}
    lines = [line for _, logger in loggers for line in logger.lines]
    return {"power_cycle": power, "captured": captured, "versions": parse_versions(lines),
            "asr": classify_asr(lines)}


def phase_verify(ctx: dict, chip) -> dict:
    started = now_bj()
    window = float(chip.verify.get("window_s", 40))
    captured = _capture_window(ctx, f"verify:{chip.id}", window)
    versions = captured["versions"]
    require_keys = list(chip.verify.get("require_keys") or [])
    missing_keys = [key for key in require_keys if not versions.get(key)]
    expectation = expectation_report(ctx, chip, versions)

    checks: dict[str, Any] = {
        "version_keys_present": not missing_keys,
        "missing_keys": missing_keys,
        "expected_version": expectation.get("match", False),
        "power_cycle": captured["power_cycle"]["verdict"],
    }
    cross = chip.verify.get("cross_check")
    if isinstance(cross, dict) and cross.get("source") in captured["captured"]:
        token = str(cross.get("token") or "")
        lines = captured["captured"][cross["source"]].get("tail") or []
        source_log = Path(captured["captured"][cross["source"]].get("log", ""))
        text = source_log.read_text(encoding="utf-8", errors="replace") if source_log.is_file() else "\n".join(lines)
        checks["cross_check"] = {"source": cross["source"], "token": token, "ok": bool(token) and token in text}

    ok = (checks["version_keys_present"] and checks["expected_version"]
          and checks["power_cycle"] == "PASS"
          and checks.get("cross_check", {}).get("ok", True))
    return {"started_at": started, "chip": chip.id, **captured, "expectation": expectation,
            "checks": checks, "verdict": "PASS" if ok else "FAIL",
            "reason": "" if ok else "version/boot checks did not match the expected baseline"}


# ---------------------------------------------------------------- recover


def phase_recover(ctx: dict, chip) -> dict:
    profile = ctx["profile"]
    started = now_bj()
    recover = chip.recover
    driver = str(recover.get("driver"))
    if driver == "none":
        return {"started_at": started, "chip": chip.id, "verdict": "SKIPPED", "reason": "recover disabled"}
    if driver == "power-cycle":
        handle = open_control(profile)
        try:
            cycle = power_cycle(handle, profile, f"recover:{chip.id}", ctx["audit"])
        finally:
            handle.close()
        return {"started_at": started, "chip": chip.id, "power_cycle": cycle,
                "verdict": cycle["verdict"]}
    if driver != "ftm-clear":
        raise BenchError(f"recover driver {driver!r} is not implemented")

    port = chip.transport[str(recover.get("port_key"))]
    baud = int(recover.get("baud"))
    serial = serial_module()
    handle = serial.Serial(port, baud, timeout=0.2, write_timeout=2)
    try:
        handle.reset_input_buffer()
        pre, started_read = b"", time.monotonic()
        while time.monotonic() - started_read < 4:
            chunk = handle.read(8192)
            if chunk:
                pre += chunk
        handle.write(str(recover["command"]).encode("ascii") + b"\r\n")
        handle.flush()
        sent_at = now_bj()
        reply, started_read = b"", time.monotonic()
        while time.monotonic() - started_read < 6:
            chunk = handle.read(8192)
            if chunk:
                reply += chunk
    finally:
        handle.close()
    text = reply.decode("utf-8", "replace")
    ack = all(token in text for token in recover.get("ack_tokens") or [])
    post = recover.get("post")
    power = None
    if post == "power-cycle":
        ctl = open_control(profile)
        try:
            power = power_cycle(ctl, profile, f"recover:{chip.id}", ctx["audit"])
        finally:
            ctl.close()
    ok = ack and (power is None or power["verdict"] == "PASS")
    return {"started_at": started, "chip": chip.id, "driver": driver, "port": f"{port}@{baud}",
            "command": recover["command"], "sent_at": sent_at, "pre_bytes": len(pre),
            "reply_bytes": len(reply), "reply_text": text[:1500], "reply_hex": reply[:200].hex(),
            "ack": ack, "power_cycle": power,
            "verdict": "PASS" if ok else "FAIL",
            "reason": "" if ok else "factory-mode exit acknowledgement missing"}


# ---------------------------------------------------------------- receipt


def _artifacts(run_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "run.receipt.json":
            continue
        out[str(path.relative_to(run_dir)).replace("\\", "/")] = sha256_file(path)
    return out


def _control_commands(run_dir: Path) -> list[dict]:
    path = run_dir / "reset_log.jsonl"
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def phase_receipt(ctx: dict, state) -> dict:
    profile = ctx["profile"]
    run_dir = Path(ctx["run_dir"])
    plan = plan_steps(profile)
    expected_steps = [step for step in plan if step != RECEIPT]
    completed = [step for step in expected_steps if state.done(step)]

    step_receipts: dict[str, Any] = {}
    versions: dict[str, Any] = {}
    for step in completed:
        path = Path((state.data.get("receipts") or {}).get(step, ""))
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            step_receipts[step] = {"verdict": payload.get("verdict"), "path": str(path)}
            if step.startswith("verify:") and payload.get("versions"):
                versions[step.split(":", 1)[1]] = payload["versions"]

    assets_report = ctx["assets"].verify_all(profile.asset_ids(), ctx.get("material_dir"), ctx.get("cache_dir"))
    control_commands = _control_commands(run_dir)
    all_steps_done = len(completed) == len(expected_steps)
    ok = all_steps_done and assets_report["all_ok"]
    receipt = {
        "issue": ctx.get("issue") or "",
        "node": profile.node,
        "profile": {"name": profile.name, "path": str(profile.path), "sha256": sha256_file(profile.path),
                    "status": profile.status, "family": profile.family},
        "time_contract": TIME_CONTRACT,
        "started_at": state.data.get("created_at") or now_bj(),
        "finished_at": now_bj(),
        "plan": plan,
        "completed_steps": completed,
        "step_receipts": step_receipts,
        "assets": assets_report,
        "versions": versions,
        "control_commands": control_commands,
        "writes": {
            "power_or_strap_commands": len(control_commands),
            "flashed_chips": [chip.id for chip in profile.chips
                              if any(step == f"burn:{chip.id}" for step in completed)],
        },
        "artifacts_sha256": _artifacts(run_dir),
        "verdict": "PASS" if ok else "FAIL",
        "reason": "" if ok else f"steps not all PASS: completed {completed} of {expected_steps}",
    }
    return receipt


def safe_recover(ctx: dict, reason: str) -> dict:
    """Best-effort bench restore after a failed step: power back on and log it.

    This never turns a FAIL into a PASS; it only prevents leaving the board
    unpowered and records exactly what was sent.
    """
    profile = ctx["profile"]
    started = now_bj()
    if str(profile.control.get("kind")) != "serial-relay" or not profile.commands.get("power_restore"):
        return {"started_at": started, "reason": reason, "skipped": True,
                "note": "no serial-relay power_restore declared in this profile"}
    try:
        handle = open_control(profile)
        try:
            record = send_control(handle, profile, "safe-recover", profile.commands["power_restore"], ctx["audit"])
        finally:
            handle.close()
    except Exception as exc:  # noqa: BLE001 - restores are best effort, the failure is reported
        return {"started_at": started, "reason": reason, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    path = Path(ctx["run_dir"]) / "recover" / "safe-recover.json"
    payload = {"started_at": started, "reason": reason, "command": record, "ok": record["receipt_ok"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return payload
