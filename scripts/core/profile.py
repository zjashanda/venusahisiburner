"""Profile loading and the isolation rules between board families.

A profile is a complete, measured description of ONE bench. There is no
profile inheritance: ``inherits`` / ``extends`` / ``defaults_from`` /
``$ref`` are rejected outright, and every port, baud rate, power command and
recovery sequence must be written down in the profile itself.

Family isolation (TCL vs Media/美的):
  * ``board_family: tcl`` must forbid the win009/CSK6 power commands
    (``uut-reset``, ``uut-csk-reset``, ``uut-burn``) so they can never be
    copied into the TCL flow by accident.
  * any other family must NOT use ``uut-*`` commands unless the profile
    explicitly sets ``control.allow_tcl_commands: true`` - the exception is
    a declaration, never an inheritance.
  * a draft profile may omit measured values, but it cannot be written to:
    the driver refuses write steps until ``status`` flips to ``measured``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contract import SCHEMAS, TIME_CONTRACT

TCL_FORBIDDEN = ["uut-reset", "uut-csk-reset", "uut-burn"]
INHERIT_KEYS = {"inherits", "extends", "defaults_from", "parent_profile", "$ref", "include"}
FAMILIES = {"tcl", "midea", "generic"}
STATUSES = {"measured", "draft"}
CONTROL_KINDS = {"serial-relay", "external-script", "manual"}
BOOT_METHODS = {"control-strap"}
DRIVERS = {"venusa-uart", "hisi-burntool", "csk6-batch"}
IMPLEMENTED_DRIVERS = {"venusa-uart", "hisi-burntool"}
RECOVER_DRIVERS = {"ftm-clear", "power-cycle", "none"}
CONTROL_KEYS = {"power_cut", "power_restore", "strap_on", "strap_off"}
CHIP_ID = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
ADDR = re.compile(r"^0x[0-9a-fA-F]+$")
COM = re.compile(r"^COM\d+$")


class ProfileError(RuntimeError):
    """Profile is missing, malformed, or violates an isolation/guard rule."""


def _need(obj: dict, key: str, where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj or obj[key] in (None, "", []):
        raise ProfileError(f"profile missing {where}.{key}")
    return obj[key]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _walk(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.append((f"{path}.{key}" if path else key, key))
            found.extend(_walk(value, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_walk(value, f"{path}[{index}]"))
    return found


class Chip:
    def __init__(self, raw: dict, profile_status: str, control: dict):
        where = f"chips[{raw.get('id', '?')}]"
        if not isinstance(raw, dict):
            raise ProfileError(f"{where} must be an object")
        self.raw = raw
        self.id = str(_need(raw, "id", where))
        if not CHIP_ID.match(self.id):
            raise ProfileError(f"{where}.id must match {CHIP_ID.pattern}, got {self.id!r}")
        self.chip = str(_need(raw, "chip", where))

        boot = _need(raw, "boot", where)
        if not isinstance(boot, dict) or not isinstance(boot.get("required"), bool):
            raise ProfileError(f"{where}.boot.required must be a boolean")
        self.boot_required = bool(boot["required"])
        if self.boot_required and str(boot.get("method")) not in BOOT_METHODS:
            raise ProfileError(f"{where}.boot.method must be one of {sorted(BOOT_METHODS)}")

        transport = _need(raw, "transport", where)
        if not isinstance(transport, dict):
            raise ProfileError(f"{where}.transport must be an object")
        self.transport = transport

        burn = _need(raw, "burn", where)
        if not isinstance(burn, dict):
            raise ProfileError(f"{where}.burn must be an object")
        self.burn = burn
        self.driver = str(_need(burn, "driver", f"{where}.burn"))
        if self.driver not in DRIVERS:
            raise ProfileError(f"{where}.burn.driver must be one of {sorted(DRIVERS)}, got {self.driver!r}")
        if profile_status == "measured":
            self.tool_asset = str(_need(burn, "tool", f"{where}.burn"))
            self.image_asset = str(_need(burn, "image", f"{where}.burn"))
        else:
            self.tool_asset = str(burn["tool"]) if burn.get("tool") else None
            self.image_asset = str(burn["image"]) if burn.get("image") else None

        verify = _need(raw, "verify", where)
        if not isinstance(verify, dict):
            raise ProfileError(f"{where}.verify must be an object")
        self.verify = verify

        recover = raw.get("recover") or {"required": False}
        if not isinstance(recover, dict) or not isinstance(recover.get("required"), bool):
            raise ProfileError(f"{where}.recover.required must be a boolean")
        self.recover = recover
        self.recover_required = bool(recover["required"])
        self.recover_before_verify = bool(recover.get("before_verify", False))
        if self.recover_required:
            if str(recover.get("driver")) not in RECOVER_DRIVERS:
                raise ProfileError(f"{where}.recover.driver must be one of {sorted(RECOVER_DRIVERS)}")
            if recover.get("driver") == "ftm-clear":
                _need(recover, "command", f"{where}.recover")
                _need(recover, "ack_tokens", f"{where}.recover")
                if str(recover.get("port_key")) not in transport:
                    raise ProfileError(f"{where}.recover.port_key must name a transport field")

        if profile_status == "measured":
            self._validate_measured(where, control)

    def _validate_measured(self, where: str, control: dict) -> None:
        log_port = self.transport.get("log_port")
        if not isinstance(log_port, str) or not COM.match(log_port):
            raise ProfileError(f"{where}.transport.log_port must be COMx for a measured profile")
        if not _is_int(self.transport.get("log_baud")) or self.transport["log_baud"] <= 0:
            raise ProfileError(f"{where}.transport.log_baud must be a positive int")
        if not str(self.transport.get("burn_port") or "").strip():
            raise ProfileError(f"{where}.transport.burn_port is required")
        if not _is_int(self.transport.get("burn_baud")) or self.transport["burn_baud"] <= 0:
            raise ProfileError(f"{where}.transport.burn_baud must be a positive int")

        if self.driver == "venusa-uart":
            if not ADDR.match(str(self.burn.get("image_addr") or "")):
                raise ProfileError(f"{where}.burn.image_addr must be 0x... for venusa-uart")
            if not self.burn.get("success_markers"):
                raise ProfileError(f"{where}.burn.success_markers must list the tool success lines")
        elif self.driver == "hisi-burntool":
            _need(self.burn, "chip_type", f"{where}.burn")
            _need(self.burn, "signal_baud", f"{where}.burn")
            if not self.burn.get("onlyburn_parts"):
                raise ProfileError(f"{where}.burn.onlyburn_parts must list the partitions to write")
            if not self.burn.get("success_tokens"):
                raise ProfileError(f"{where}.burn.success_tokens must list the optLog success words")
        elif self.driver == "csk6-batch":
            raise ProfileError(
                f"{where}.burn.driver csk6-batch is declared but not implemented/verified on this node; "
                "keep status=draft until a Media bench is measured")

        version_key = str(_need(self.verify, "version_key", f"{where}.verify"))
        if not self.verify.get("require_keys"):
            raise ProfileError(f"{where}.verify.require_keys must list the parsed version keys")
        if version_key not in self.verify["require_keys"]:
            raise ProfileError(f"{where}.verify.require_keys must contain version_key {version_key!r}")
        if not str(self.verify.get("expect") or "").strip():
            raise ProfileError(f"{where}.verify.expect is required for a measured profile")
        if not isinstance(self.verify.get("window_s"), (int, float)) or self.verify["window_s"] <= 0:
            raise ProfileError(f"{where}.verify.window_s must be a positive number")

    @property
    def log_port(self) -> str | None:
        return self.transport.get("log_port")

    @property
    def log_baud(self) -> int | None:
        return self.transport.get("log_baud")

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chip": self.chip,
            "driver": self.driver,
            "log_port": self.log_port,
            "log_baud": self.log_baud,
            "burn_port": self.transport.get("burn_port"),
            "burn_baud": self.transport.get("burn_baud"),
            "boot": {"required": self.boot_required},
            "recover": {"required": self.recover_required,
                        "before_verify": self.recover_before_verify,
                        "driver": self.recover.get("driver") if self.recover_required else None},
            "verify": {"version_key": self.verify.get("version_key"), "expect": self.verify.get("expect")},
            "assets": [asset_id for asset_id in (self.tool_asset, self.image_asset) if asset_id],
        }


class Profile:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ProfileError(
                f"profile not found: {self.path}. 没有明确 profile 不允许烧录；先把现场实测的端口/波特率/断电方式写进 profile。")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProfileError(f"profile is not valid JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProfileError("profile root must be an object")
        self.raw = raw
        self._validate(raw)

    # ------------------------------------------------------------ checks
    def _validate(self, raw: dict) -> None:
        if raw.get("schema") != SCHEMAS["profile"]:
            raise ProfileError(f"profile schema must be {SCHEMAS['profile']}, got {raw.get('schema')!r}")
        for key, _ in _walk(raw):
            leaf = key.rsplit(".", 1)[-1].split("[")[0]
            if leaf in INHERIT_KEYS:
                raise ProfileError(
                    f"profile uses {leaf!r} at {key}: profiles never inherit from each other; "
                    "declare every port/baud/power/recover value in this file")

        for key in ("name", "node", "board_family", "board", "status", "time_contract", "control", "chips"):
            _need(raw, key, "profile")
        if raw["board_family"] not in FAMILIES:
            raise ProfileError(f"board_family must be one of {sorted(FAMILIES)}")
        if raw["status"] not in STATUSES:
            raise ProfileError(f"status must be one of {sorted(STATUSES)}")
        if raw["time_contract"] != "beijing-no-offset":
            raise ProfileError(
                f"time_contract must be 'beijing-no-offset' ({TIME_CONTRACT['format']}), got {raw['time_contract']!r}")

        control = raw["control"]
        if not isinstance(control, dict):
            raise ProfileError("control must be an object")
        kind = str(_need(control, "kind", "control"))
        if kind not in CONTROL_KINDS:
            raise ProfileError(f"control.kind must be one of {sorted(CONTROL_KINDS)}")
        commands = control.get("commands") or {}
        if kind == "serial-relay":
            if not isinstance(commands, dict):
                raise ProfileError("control.commands must be an object")
            if not COM.match(str(_need(control, "port", "control"))):
                raise ProfileError("control.port must be COMx for serial-relay")
            if not _is_int(control.get("baud")) or control["baud"] <= 0:
                raise ProfileError("control.baud must be a positive int for serial-relay")
            for key in ("power_cut", "power_restore"):
                _need(commands, key, "control.commands")
            unknown = set(commands) - CONTROL_KEYS
            if unknown:
                raise ProfileError(f"control.commands has unknown keys: {sorted(unknown)}")
            if raw["status"] == "measured":
                timing = _need(control, "timing", "control")
                for key in ("cut_to_strap_s", "strap_to_restore_s", "restore_to_strap_off_s", "cycle_hold_s"):
                    if not isinstance(timing.get(key), (int, float)) or timing[key] < 0:
                        raise ProfileError(f"control.timing.{key} must be a non-negative number")

        self._validate_isolation(raw, control, commands)

        chips_raw = raw["chips"]
        if not isinstance(chips_raw, list) or not chips_raw:
            raise ProfileError("chips must be a non-empty list")
        chips = [Chip(item, raw["status"], control) for item in chips_raw]
        ids = [chip.id for chip in chips]
        if len(ids) != len(set(ids)):
            raise ProfileError(f"chip ids must be unique, got {ids}")
        self.chips: list[Chip] = chips
        if raw["status"] == "draft":
            _need(raw, "measurement_required", "profile")

        for chip in chips:
            if chip.boot_required and "strap_on" not in commands:
                raise ProfileError(f"chip {chip.id} needs control.commands.strap_on but it is not declared")
            if chip.boot_required and "strap_off" not in commands:
                raise ProfileError(f"chip {chip.id} needs control.commands.strap_off but it is not declared")

        self.control: dict = control
        self.commands: dict = commands

    @staticmethod
    def _validate_isolation(raw: dict, control: dict, commands: dict) -> None:
        forbidden = list(control.get("forbidden_commands") or [])
        used = {str(value) for value in commands.values()}
        hit = used & set(forbidden)
        if hit:
            raise ProfileError(f"control.commands uses forbidden command(s): {sorted(hit)}")
        if raw["board_family"] == "tcl":
            missing = [cmd for cmd in TCL_FORBIDDEN if cmd not in forbidden]
            if missing:
                raise ProfileError(f"TCL profile must forbid {missing} (win009/CSK6 commands)")
        else:
            uut = sorted(cmd for cmd in used if cmd.startswith("uut-"))
            if uut and not control.get("allow_tcl_commands"):
                raise ProfileError(
                    f"board_family={raw['board_family']} may not use TCL commands {uut} unless "
                    "control.allow_tcl_commands=true is declared explicitly")

    # ------------------------------------------------------------ helpers
    @property
    def name(self) -> str:
        return str(self.raw["name"])

    @property
    def node(self) -> str:
        return str(self.raw["node"])

    @property
    def family(self) -> str:
        return str(self.raw["board_family"])

    @property
    def status(self) -> str:
        return str(self.raw["status"])

    @property
    def measured(self) -> bool:
        return self.status == "measured"

    def chip(self, chip_id: str) -> Chip:
        for chip in self.chips:
            if chip.id == chip_id:
                return chip
        raise ProfileError(f"unknown chip {chip_id!r}; profile declares {[c.id for c in self.chips]}")

    def expected_ports(self) -> list[str]:
        ports: list[str] = []
        control_port = self.control.get("port")
        if isinstance(control_port, str) and COM.match(control_port):
            ports.append(control_port)
        for chip in self.chips:
            if chip.log_port:
                ports.append(chip.log_port)
        return sorted(set(ports))

    def asset_ids(self) -> list[str]:
        ids: list[str] = []
        for chip in self.chips:
            for asset_id in (chip.tool_asset, chip.image_asset):
                if asset_id and asset_id not in ids:
                    ids.append(asset_id)
        return ids

    def describe(self) -> dict[str, Any]:
        return {
            "profile": str(self.path),
            "name": self.name,
            "node": self.node,
            "family": self.family,
            "board": self.raw.get("board"),
            "status": self.status,
            "time_contract": TIME_CONTRACT,
            "control": {
                "kind": self.control.get("kind"),
                "port": self.control.get("port"),
                "baud": self.control.get("baud"),
                "commands": dict(self.commands),
            },
            "chips": [chip.describe() for chip in self.chips],
            "expected_ports": self.expected_ports(),
            "measurement_required": self.raw.get("measurement_required") or [],
        }
