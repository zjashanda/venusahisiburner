"""Bench primitives: exclusive lock, serial relay, port logger, tool runner.

Nothing here decides PASS/FAIL for a phase - it only produces evidence.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .contract import clean, now_bj


class BenchError(RuntimeError):
    """Bench is busy/unavailable, or a tool could not be started."""


def _pid_alive(pid: int) -> bool:
    """True when the pid still runs. Never signals the process (os.kill on Windows terminates)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415 - Windows only

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def default_lock_path(node: str) -> Path:
    return Path(tempfile.gettempdir()) / f"venusahisburner-{node}.lock"


class BenchLock:
    """Exclusive per-node lock: two runs must never drive one bench at once.

    A second run is refused instead of fighting for COM ports - the other
    run may be another agent instance, and killing it is not allowed.
    """

    def __init__(self, node: str, path: Path | None = None):
        self.path = Path(path) if path else default_lock_path(node)
        self.held = False

    def __enter__(self) -> "BenchLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read_holder()
                if holder and _pid_alive(int(holder.get("pid", -1))):
                    raise BenchError(
                        f"bench is already locked by pid {holder['pid']} since {holder.get('since', '?')} "
                        f"({self.path}); 另一轮正在操作台架，不抢占、不结束其他进程，等它结束后再跑")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "since": now_bj()}, handle)
            self.held = True
            return self
        raise BenchError(f"could not acquire bench lock {self.path}")

    def _read_holder(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def __exit__(self, *exc_info) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class Audit:
    """Append-only reset_log.jsonl: every bench control command with its raw reply."""

    def __init__(self, run_dir: Path, issue: str):
        self.run_dir = Path(run_dir)
        self.issue = issue
        self.path = self.run_dir / "reset_log.jsonl"
        self.entries: list[dict[str, Any]] = []

    def control(self, step: str, record: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": now_bj(), "issue": self.issue, "step": step,
            "command": record.get("command"), "receipt_ok": record.get("receipt_ok"),
            "reply": (record.get("reply") or [])[:8],
            "note": "power/boot control via the profile-declared sequence only",
        }
        self.entries.append(entry)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def serial_module():
    try:
        import serial  # noqa: PLC0415
        import serial.tools.list_ports  # noqa: F401,PLC0415

        return serial
    except ImportError as exc:  # pragma: no cover - environment guard
        raise BenchError("pyserial is required for hardware steps: pip install pyserial") from exc


def expect_token(control: dict, command: str) -> str:
    for prefix, token in (control.get("receipt_tokens") or {}).items():
        if command.startswith(prefix):
            return token
    fallback = control.get("receipt_fallback") or control.get("receipt_expect")
    if not fallback:
        raise BenchError(f"no receipt token declared for {command!r} (control.receipt_tokens / receipt_fallback)")
    return fallback


def line_end_bytes(line_ending: str) -> bytes:
    return {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r", "none": b""}.get(line_ending, b"\r\n")


def relay(handle, command: str, expect: str, line_ending: str = "crlf", timeout_s: float = 5.0) -> dict:
    handle.reset_input_buffer()
    handle.write(command.encode("ascii") + line_end_bytes(line_ending))
    handle.flush()
    started = time.monotonic()
    buf, reply = b"", []
    ok = False
    while time.monotonic() - started < timeout_s:
        chunk = handle.read(256)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = clean(raw.decode("utf-8", "replace"))
            reply.append(line)
            if expect in line:
                ok = True
        if ok:
            break
    return {"command": command, "at": now_bj(), "receipt_ok": ok, "reply": reply[:8],
            "elapsed_s": round(time.monotonic() - started, 3)}


def open_control(profile) -> Any:
    serial = serial_module()
    control = profile.control
    return serial.Serial(control["port"], control["baud"], timeout=0.2, write_timeout=2)


def send_control(handle, profile, step: str, command: str, audit: Audit, timeout_s: float = 5.0) -> dict:
    control = profile.control
    record = relay(handle, command, expect_token(control, command),
                   str(control.get("line_ending") or "crlf"), timeout_s)
    audit.control(step, record)
    return record


def power_cycle(handle, profile, step: str, audit: Audit) -> dict:
    """Cut and restore power through the profile's declared control sequence."""
    timing = profile.control.get("timing") or {}
    hold = float(timing.get("cycle_hold_s", 2.5))
    cut = send_control(handle, profile, step, profile.commands["power_cut"], audit)
    time.sleep(max(hold, 0.5))
    restore = send_control(handle, profile, step, profile.commands["power_restore"], audit)
    ok = bool(cut["receipt_ok"] and restore["receipt_ok"])
    return {"cut": cut, "restore": restore, "verdict": "PASS" if ok else "FAIL"}


class PortLogger(threading.Thread):
    """Mirror one serial port to a UTF-8 log for the duration of a step."""

    def __init__(self, port: str, baud: int, log_path: Path):
        super().__init__(daemon=True)
        self.port, self.baud, self.log_path = port, baud, Path(log_path)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.bytes = 0
        self.error = ""

    def run(self) -> None:
        serial = serial_module()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            while not self.stop_event.is_set():
                try:
                    with serial.Serial(self.port, self.baud, timeout=0.2) as port:
                        port.reset_input_buffer()
                        self.error = ""
                        buf = b""
                        while not self.stop_event.is_set():
                            chunk = port.read(8192)
                            if not chunk:
                                continue
                            with self.lock:
                                self.bytes += len(chunk)
                            buf += chunk
                            while b"\n" in buf:
                                raw, buf = buf.split(b"\n", 1)
                                line = clean(raw.decode("utf-8", "replace"))
                                with self.lock:
                                    self.lines.append(line)
                                handle.write(f"[{now_bj()}] {line}\n")
                                handle.flush()
                except Exception as exc:  # noqa: BLE001 - surfaced in the phase receipt
                    self.error = f"{type(exc).__name__}: {exc}"
                    handle.write(f"[{now_bj()}] ### PORT_ERROR {self.error}\n")
                    handle.flush()
                if self.stop_event.is_set():
                    break
                time.sleep(0.8)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"port": self.port, "baud": self.baud, "log": str(self.log_path),
                    "bytes": self.bytes, "lines": len(self.lines), "error": self.error,
                    "tail": self.lines[-10:]}

    def find(self, pattern: str) -> list[str]:
        regex = re.compile(pattern, re.I)
        with self.lock:
            return [line for line in self.lines if regex.search(line)]


def run_tool(argv: list[str], cwd: Path, timeout_s: float) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run([str(item) for item in argv], cwd=str(cwd), capture_output=True, timeout=timeout_s)
        text = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
        if "\ufffd" in text:
            text = (proc.stdout or b"").decode("gb18030", "replace") + (proc.stderr or b"").decode("gb18030", "replace")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or b"").decode("gb18030", "replace") if exc.stdout else ""
        text += "\n<<TIMEOUT>>"
        code = -9
    return {"cmd": [Path(str(argv[0])).name] + [str(item) for item in argv[1:]], "rc": code,
            "elapsed_s": round(time.monotonic() - started, 2), "output": text}


def stderr(message: str) -> None:
    print(f"venusahisiburner: {message}", file=sys.stderr)
