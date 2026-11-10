"""Shared contracts for venusahisiburner.

Everything that both the driver and the tests must agree on lives here:
schemas, the Beijing wall-clock time contract, hashing helpers and the
version parsers for the two MCUs.

Time contract (hard): every timestamp written by this skill is Beijing
wall-clock time with NO timezone offset in the string, e.g.
``2026-11-10T14:05:00.123``. ``+08:00`` suffixes and UTC strings are a
contract violation and are rejected by ``is_bj_timestamp``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMAS = {
    "profile": "venusahisiburner-profile/v1",
    "assets": "venusahisiburner-assets/v1",
    "state": "venusahisiburner-state/v1",
    "phase": "venusahisiburner-phase/v1",
    "receipt": "venusahisiburner-receipt/v1",
}

BJ = timezone(timedelta(hours=8), "Asia/Shanghai")
TIME_CONTRACT: dict[str, str] = {
    "zone": "Asia/Shanghai",
    "offset_in_string": "none",
    "format": "YYYY-MM-DDThh:mm:ss.sss",
    "example": "2026-11-10T14:05:00.123",
}

_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$")
_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%f"

ANSI = re.compile(r"\x1b\[[0-9;]*m")

VERSION_PATTERNS = {
    "csk_app": re.compile(r"asr_dsp/SP-LVM141-A-ChXx-FX-V\d+\.\d+\.\d+"),
    "csk_boot": re.compile(r"boot version:\s*(\S+)"),
    "csk_player": re.compile(r"Lisa Player Version\s+(\S+)"),
    "hisi_sys_ver": re.compile(r"SYS_VER:\s*(\S+)"),
    "hisi_sdk_ver": re.compile(r"SDK_VER:\s*(\S+)"),
    "hisi_commit": re.compile(r"Commit:\s*([0-9a-f]{6,}),\s*Compiler:\s*(.+)$"),
}
ASR_FIXED = re.compile(r"use_fixed_asr\s*[:=]\s*(\d)")
ASR_SERVER = re.compile(r"fixed_server\s*[:=]\s*(\d)")


class ContractError(RuntimeError):
    """A contract violation: bad timestamp, bad schema, bad hash."""


def now_bj() -> str:
    """Current Beijing wall-clock time, millisecond precision, no offset."""
    return datetime.now(BJ).strftime(_TIME_FMT)[:-3]


def parse_bj(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TIME_FMT).replace(tzinfo=BJ)
    except (TypeError, ValueError):
        return None


def is_bj_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        return False
    return parse_bj(value) is not None


def require_bj_timestamp(value: Any, where: str) -> str:
    if not is_bj_timestamp(value):
        raise ContractError(f"{where}: {value!r} is not a Beijing no-offset timestamp ({TIME_CONTRACT['format']})")
    return value


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    """Write JSON atomically, UTF-8, no BOM, no promised line endings lost."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def clean(text: str) -> str:
    return ANSI.sub("", text).replace("\r", "").strip()


def read_text_any(path: Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_versions(lines: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in lines:
        for key, pattern in VERSION_PATTERNS.items():
            if key in found:
                continue
            match = pattern.search(line)
            if not match:
                continue
            found[key] = match.group(1) if match.groups() else match.group(0)
    return found


def classify_asr(lines: list[str]) -> dict[str, Any]:
    """Classify which recognizer produced the intent.

    ``use_fixed_asr:0`` / ``fixed_server:0`` means the upper MCU chose the
    cloud ASR, which is a link condition - never an offline-intent failure.
    """
    text = "\n".join(lines)
    fixed = [int(value) for value in ASR_FIXED.findall(text)]
    server = [int(value) for value in ASR_SERVER.findall(text)]
    if fixed and all(value == 0 for value in fixed):
        return {"path": "cloud", "evidence": f"use_fixed_asr={fixed[-1]}", "fixed_asr": fixed}
    if fixed and all(value == 1 for value in fixed):
        return {"path": "offline", "evidence": f"use_fixed_asr={fixed[-1]}", "fixed_asr": fixed}
    if server and all(value == 0 for value in server):
        return {"path": "cloud", "evidence": f"fixed_server={server[-1]}", "fixed_server": server}
    return {"path": "unknown", "evidence": "no use_fixed_asr/fixed_server marker", "fixed_asr": fixed}


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
