"""Resolve a device-io JSON document into this skill's serial roles.

device-io is the only source of device truth: this module reads the JSON it
produces and never enumerates USB/serial ports itself. Two documents are
accepted, dispatched on the ``schema`` field:

  * ``ark.device-io.v1``       - raw port snapshot; a role is matched by the
    port name (``COM6``, ``/dev/ttyUSB0``) or interface label already declared
    in the profile, by exact case-insensitive equality only.
  * ``ark.device-role-map.v1`` - ``device-io roles`` output; ``resolved`` and
    ``pinned`` roles are used directly, every other state is a hard error.

Roles for this skill's profile:

  control          profile ``control.port`` (only when it declares one)
  <chip>:log       ``chips[].transport.log_port``
  <chip>:burn      ``chips[].transport.burn_port``

A single-chip profile also accepts the bare roles ``log`` / ``burn``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "ark.device-io.v1"
ROLE_MAP_SCHEMA = "ark.device-role-map.v1"
GOOD_STATES = ("resolved", "pinned")


class DeviceSnapshotError(RuntimeError):
    """The --device-snapshot document is unreadable or a role cannot be resolved."""


def _fail(path: Path, message: str) -> None:
    raise DeviceSnapshotError(f"device snapshot {path}: {message}")


def com_number(port: str) -> str:
    """``COM4`` -> ``4`` for BurnTool's ``-com:`` flag; other names unchanged."""
    text = str(port).strip()
    if text.lower().startswith("com") and text[3:].isdigit():
        return text[3:]
    return text


def _load(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise DeviceSnapshotError(f"device snapshot not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeviceSnapshotError(f"device snapshot is not readable JSON: {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise DeviceSnapshotError(f"device snapshot root must be an object: {path}")
    return doc


def _targets(profile) -> list[tuple[str, str, str | None]]:
    """(role name, declared port, bare alias) for every role this flow needs."""
    single = len(profile.chips) == 1
    targets: list[tuple[str, str, str | None]] = []
    for chip in profile.chips:
        for kind in ("log", "burn"):
            value = chip.transport.get(f"{kind}_port")
            declared = str(value) if value not in (None, "") else ""
            targets.append((f"{chip.id}:{kind}", declared, kind if single else None))
    control = profile.control.get("port")
    if control not in (None, ""):
        targets.append(("control", str(control), None))
    return targets


def _resolve_role_map(path: Path, doc: dict, targets: list[tuple[str, str, str | None]],
                      multi_chip: bool) -> dict:
    model = doc.get("roles")
    if not isinstance(model, dict):
        _fail(path, "role-map has no 'roles' object")
    if doc.get("ok") is False:
        bad = [f"{name}={entry.get('state')}" for name, entry in model.items()
               if not isinstance(entry, dict) or entry.get("state") not in GOOD_STATES]
        _fail(path, f"role-map ok=false, unresolved roles: {', '.join(bad) or 'no detail'}")
    names = set(model)
    if multi_chip and ({"log", "burn"} & names):
        chips = sorted({target[0].split(":", 1)[0] for target in targets if ":" in target[0]})
        _fail(path, f"role-map uses a bare 'log'/'burn' role but the profile has chips {chips}; "
                    "declare per-chip roles ('<chip>:log' / '<chip>:burn') instead")

    roles: dict[str, dict[str, Any]] = {}
    for role, _declared, alias in targets:
        key = role if role in model else (alias if alias in names else None)
        if key is None:
            _fail(path, f"role '{role}' missing from role-map (declared: {sorted(model)})")
        entry = model[key]
        if not isinstance(entry, dict):
            _fail(path, f"role '{role}' is not an object")
        state = str(entry.get("state") or "")
        reason = str(entry.get("reason") or "")
        port = entry.get("port")
        if state in GOOD_STATES and isinstance(port, str) and port.strip():
            roles[role] = {"state": state, "port": port.strip(), "reason": reason}
            continue
        _fail(path, f"role '{role}' state '{state or 'unknown'}': {reason or 'no usable port'}")
    return roles


def _match(ports: list, declared: str) -> list[dict]:
    low = declared.strip().lower()
    numeric = low.isdigit()
    hits: list[dict] = []
    for port in ports:
        if not isinstance(port, dict) or port.get("present") is False:
            continue
        device = str(port.get("device") or "").strip().lower()
        interface = str(port.get("interface") or "").strip().lower()
        if low and low in (device, interface):
            hits.append(port)
        elif numeric and device == "com" + low:
            hits.append(port)  # BurnTool's -com:<n> form names the same COM device
    unique: dict[str, dict] = {}
    for port in hits:
        unique[str(port.get("device") or port.get("id"))] = port
    return list(unique.values())


def _resolve_snapshot(path: Path, doc: dict, targets: list[tuple[str, str, str | None]]) -> dict:
    ports = doc.get("ports")
    if not isinstance(ports, list):
        _fail(path, "snapshot has no 'ports' list")
    available = sorted({str(port.get("device")) for port in ports
                        if isinstance(port, dict) and port.get("present") is not False})
    roles: dict[str, dict[str, Any]] = {}
    for role, declared, _alias in targets:
        if not declared:
            _fail(path, f"role '{role}' has no declared port in the profile; a raw snapshot matches "
                        "declared names only, use a role-map document instead")
        hits = _match(ports, declared)
        if not hits:
            _fail(path, f"role '{role}' state 'missing': no port device/interface equals "
                        f"'{declared}' (present: {available})")
        if len(hits) > 1:
            names = ", ".join(str(port.get("device")) for port in hits)
            _fail(path, f"role '{role}' state 'ambiguous': '{declared}' matched {names}")
        roles[role] = {"state": "resolved", "port": str(hits[0].get("device")),
                       "reason": f"matched declared name '{declared}'"}
    return roles


def resolve_snapshot(path: Path, profile) -> dict:
    """Return ``{source, schema, generated_at, mode, roles}`` or raise a hard error."""
    path = Path(path)
    doc = _load(path)
    schema = doc.get("schema")
    targets = _targets(profile)
    if schema == ROLE_MAP_SCHEMA:
        role_entries = _resolve_role_map(path, doc, targets, len(profile.chips) > 1)
        mode = "role-map"
    elif schema == SNAPSHOT_SCHEMA:
        role_entries, mode = _resolve_snapshot(path, doc, targets), "snapshot"
    else:
        _fail(path, f"unsupported schema {schema!r}; expected {SNAPSHOT_SCHEMA} or {ROLE_MAP_SCHEMA}")
    generated = doc.get("generated_at")
    return {
        "source": str(path),
        "schema": str(schema),
        "generated_at": generated if isinstance(generated, str) else "",
        "mode": mode,
        "roles": role_entries,
    }


def apply_snapshot(profile, resolved: dict) -> None:
    """Overwrite the profile's ports with the resolved snapshot ports in place."""
    ports = {role: str(item["port"]) for role, item in (resolved.get("roles") or {}).items()}
    single = len(profile.chips) == 1
    for chip in profile.chips:
        for kind in ("log", "burn"):
            port = ports.get(f"{chip.id}:{kind}")
            if port is None and single:
                port = ports.get(kind)
            if port is not None:
                chip.transport[f"{kind}_port"] = port
    if "control" in ports:
        profile.control["port"] = ports["control"]
