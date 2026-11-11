"""device-io snapshot / role-map consumption tests - offline, no hardware."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core.device_snapshot import (DeviceSnapshotError, apply_snapshot,  # noqa: E402
                                  resolve_snapshot)
from core.profile import Profile  # noqa: E402

TCL = ROOT / "scripts" / "profiles" / "tcl-pvm1005.json"
MIDEA = ROOT / "scripts" / "profiles" / "midea-csk6.json"
STAMP = "2026-11-11 18:00:00"


class FakeChip:
    def __init__(self, chip_id: str, log_port=None, burn_port=None):
        self.id = chip_id
        self.transport = {"log_port": log_port, "burn_port": burn_port}


class FakeProfile:
    def __init__(self, chips, control=None):
        self.chips = chips
        self.control = control or {}


def role_map(roles: dict, ok: bool = True) -> dict:
    return {"schema": "ark.device-role-map.v1", "generated_at": STAMP,
            "spec_schema": "ark.device-roles-spec.v1", "ok": ok, "roles": roles}


def entry(state: str, port=None, reason: str = "") -> dict:
    return {"role": "x", "state": state, "port": port, "reason": reason}


def snapshot(ports: list[dict]) -> dict:
    return {"schema": "ark.device-io.v1", "generated_at": STAMP,
            "backend": "fixture", "os": "windows", "ports": ports, "audio": [], "warnings": []}


def port(device: str, interface: str = "", present: bool = True) -> dict:
    return {"device": device, "interface": interface, "present": present}


class DocCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, payload: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


class RoleMapTests(DocCase):
    def full_roles(self) -> dict:
        return {
            "control": entry("pinned", "COM16", "caller pinned COM16"),
            "venusa:log": entry("resolved", "COM13", "matched SERIAL-A"),
            "venusa:burn": entry("resolved", "COM17", "matched SERIAL-A"),
            "hisi:log": entry("resolved", "COM15", "matched SERIAL-B"),
            "hisi:burn": entry("resolved", "COM18", "matched SERIAL-B"),
        }

    def test_role_map_overrides_every_profile_port(self):
        profile = Profile(TCL)
        resolved = resolve_snapshot(self.write("roles.json", role_map(self.full_roles())), profile)
        self.assertEqual(resolved["mode"], "role-map")
        self.assertEqual(resolved["generated_at"], STAMP)
        self.assertEqual(resolved["roles"]["hisi:burn"]["port"], "COM18")
        apply_snapshot(profile, resolved)
        self.assertEqual(profile.control["port"], "COM16")
        self.assertEqual(profile.chip("venusa").transport["log_port"], "COM13")
        self.assertEqual(profile.chip("venusa").transport["burn_port"], "COM17")
        self.assertEqual(profile.chip("hisi").transport["log_port"], "COM15")
        self.assertEqual(profile.chip("hisi").transport["burn_port"], "COM18")

    def test_missing_role_is_an_error(self):
        roles = self.full_roles()
        del roles["hisi:burn"]
        path = self.write("roles.json", role_map(roles))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        self.assertIn("hisi:burn", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))

    def test_missing_state_names_role_state_and_reason(self):
        roles = self.full_roles()
        roles["hisi:log"] = entry("missing", None, "no port matched SERIAL-B")
        path = self.write("roles.json", role_map(roles))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        text = str(ctx.exception)
        self.assertIn("hisi:log", text)
        self.assertIn("missing", text)
        self.assertIn("no port matched SERIAL-B", text)

    def test_ambiguous_state_is_an_error(self):
        roles = self.full_roles()
        roles["venusa:log"] = entry("ambiguous", None, "2 ports matched SERIAL-A")
        path = self.write("roles.json", role_map(roles))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        self.assertIn("ambiguous", str(ctx.exception))
        self.assertIn("venusa:log", str(ctx.exception))

    def test_conflict_state_is_an_error(self):
        roles = self.full_roles()
        roles["hisi:burn"] = entry("conflict", None, "COM18 claimed by roles: hisi:log, hisi:burn")
        path = self.write("roles.json", role_map(roles))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        self.assertIn("conflict", str(ctx.exception))
        self.assertIn("hisi:burn", str(ctx.exception))

    def test_ok_false_lists_unresolved_roles(self):
        roles = self.full_roles()
        roles["venusa:burn"] = entry("missing", None, "no port matched SERIAL-A")
        path = self.write("roles.json", role_map(roles, ok=False))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        text = str(ctx.exception)
        self.assertIn("ok=false", text)
        self.assertIn("venusa:burn", text)

    def test_bare_roles_are_refused_for_a_dual_chip_profile(self):
        path = self.write("roles.json", role_map({"log": entry("resolved", "COM3")}))
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(path, Profile(TCL))
        self.assertIn("per-chip", str(ctx.exception))

    def test_bare_roles_work_for_a_single_chip_profile(self):
        profile = Profile(MIDEA)
        path = self.write("roles.json", role_map({"log": entry("resolved", "COM7"),
                                                  "burn": entry("resolved", "COM8")}))
        resolved = resolve_snapshot(path, profile)
        apply_snapshot(profile, resolved)
        self.assertEqual(profile.chip("csk6").transport["log_port"], "COM7")
        self.assertEqual(profile.chip("csk6").transport["burn_port"], "COM8")


class RawSnapshotTests(DocCase):
    def test_snapshot_matches_declared_names_and_numeric_com(self):
        profile = Profile(TCL)
        doc = snapshot([port("COM3", "SERIAL-A"), port("COM4", "SERIAL-B"), port("COM6", "VCOM")])
        resolved = resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertEqual(resolved["mode"], "snapshot")
        self.assertEqual(resolved["roles"]["venusa:log"]["port"], "COM3")
        self.assertEqual(resolved["roles"]["venusa:burn"]["port"], "COM3")
        self.assertEqual(resolved["roles"]["hisi:log"]["port"], "COM4")
        self.assertEqual(resolved["roles"]["hisi:burn"]["port"], "COM4")
        self.assertEqual(resolved["roles"]["control"]["port"], "COM6")
        apply_snapshot(profile, resolved)
        self.assertEqual(profile.chip("hisi").transport["burn_port"], "COM4")

    def test_snapshot_matches_interface_labels_case_insensitively(self):
        profile = FakeProfile([FakeChip("x", log_port="serial-a", burn_port="COM30")])
        doc = snapshot([port("COM21", "SERIAL-A"), port("COM30", "SERIAL-B")])
        resolved = resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertEqual(resolved["roles"]["x:log"]["port"], "COM21")

    def test_snapshot_missing_port_is_an_error(self):
        profile = Profile(TCL)
        doc = snapshot([port("COM3", "SERIAL-A"), port("COM6", "VCOM")])
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertIn("hisi:log", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))

    def test_snapshot_ambiguous_match_is_an_error(self):
        profile = FakeProfile([FakeChip("x", log_port="SERIAL-A")])
        doc = snapshot([port("COM21", "SERIAL-A"), port("COM22", "SERIAL-A")])
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertIn("ambiguous", str(ctx.exception))
        self.assertIn("x:log", str(ctx.exception))

    def test_snapshot_never_matches_a_fuzzy_name(self):
        profile = FakeProfile([FakeChip("x", log_port="COM1")])
        doc = snapshot([port("COM10", ""), port("COM11", "")])
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertIn("missing", str(ctx.exception))

    def test_snapshot_without_a_declared_port_points_at_role_map(self):
        profile = FakeProfile([FakeChip("x")])
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(self.write("snap.json", snapshot([port("COM1")])), profile)
        self.assertIn("role-map", str(ctx.exception))

    def test_absent_ports_are_not_candidates(self):
        profile = FakeProfile([FakeChip("x", log_port="COM5")])
        doc = snapshot([port("COM5", present=False)])
        with self.assertRaises(DeviceSnapshotError) as ctx:
            resolve_snapshot(self.write("snap.json", doc), profile)
        self.assertIn("missing", str(ctx.exception))


class CliSnapshotTests(DocCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_no_flag_keeps_the_profile_ports(self):
        code, out, _err = self.run_cli(["plan", "--profile", str(TCL), "--run-dir", str(self.tmp)])
        self.assertEqual(code, 0)
        self.assertIn("control: serial-relay COM6@115200", out)
        self.assertNotIn("device snapshot", out)

    def test_plan_prints_the_snapshot_block(self):
        roles = {
            "control": entry("pinned", "COM16", "caller pinned COM16"),
            "venusa:log": entry("resolved", "COM13", "matched SERIAL-A"),
            "venusa:burn": entry("resolved", "COM17", "matched SERIAL-A"),
            "hisi:log": entry("resolved", "COM15", "matched SERIAL-B"),
            "hisi:burn": entry("resolved", "COM18", "matched SERIAL-B"),
        }
        path = self.write("roles.json", role_map(roles))
        code, out, _err = self.run_cli(["plan", "--profile", str(TCL), "--run-dir", str(self.tmp),
                                        "--device-snapshot", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("device snapshot:", out)
        self.assertIn("mode: role-map", out)
        self.assertIn("control: COM16 (pinned)", out)
        self.assertIn("venusa:log: COM13 (resolved)", out)
        self.assertIn("control: serial-relay COM16@115200", out)

    def test_unresolvable_snapshot_exits_2_with_one_line(self):
        roles = {
            "control": entry("pinned", "COM16", "caller pinned COM16"),
            "venusa:log": entry("resolved", "COM13", "matched SERIAL-A"),
            "venusa:burn": entry("resolved", "COM17", "matched SERIAL-A"),
            "hisi:log": entry("missing", None, "no port matched SERIAL-B"),
            "hisi:burn": entry("resolved", "COM18", "matched SERIAL-B"),
        }
        path = self.write("roles.json", role_map(roles))
        code, out, err = self.run_cli(["plan", "--profile", str(TCL), "--run-dir", str(self.tmp),
                                       "--device-snapshot", str(path)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("hisi:log", err)

    def test_preflight_receipt_carries_the_snapshot_block(self):
        path = self.write("roles.json", role_map({"log": entry("resolved", "COM7"),
                                                  "burn": entry("resolved", "COM8")}))
        code, _out, _err = self.run_cli(["preflight", "--profile", str(MIDEA),
                                         "--run-dir", str(self.tmp),
                                         "--device-snapshot", str(path)])
        receipt = json.loads((self.tmp / "steps" / "preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 4)
        self.assertEqual(receipt["verdict"], "BLOCKED")
        block = receipt["device_snapshot"]
        self.assertEqual(block["mode"], "role-map")
        self.assertEqual(block["schema"], "ark.device-role-map.v1")
        self.assertEqual(block["roles"]["csk6:log"]["port"], "COM7")

    def test_preflight_without_a_flag_has_no_snapshot_key(self):
        code, _out, _err = self.run_cli(["preflight", "--profile", str(MIDEA),
                                         "--run-dir", str(self.tmp)])
        receipt = json.loads((self.tmp / "steps" / "preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 4)
        self.assertNotIn("device_snapshot", receipt)


if __name__ == "__main__":
    unittest.main()
