"""Profile guard and TCL/Media isolation tests - no hardware involved."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from core.profile import Profile, ProfileError  # noqa: E402
from core.state import plan_steps  # noqa: E402

TCL = ROOT / "scripts" / "profiles" / "tcl-pvm1005.json"
MIDEA = ROOT / "scripts" / "profiles" / "midea-csk6.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_variant(raw: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(raw, tmp, ensure_ascii=False)
    tmp.close()
    return Path(tmp.name)


def strings(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            out.extend(strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(strings(item))
    elif isinstance(value, str):
        out.append(value)
    return out


class ProfileGuardTests(unittest.TestCase):
    def test_missing_profile_is_refused(self):
        with self.assertRaises(ProfileError) as ctx:
            Profile(Path("does-not-exist.json"))
        self.assertIn("没有明确 profile", str(ctx.exception))

    def test_schema_mismatch_is_refused(self):
        raw = load(TCL)
        raw["schema"] = "tcl-autoburn-profile/v1"
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("schema", str(ctx.exception))
        path.unlink()

    def test_time_contract_must_be_beijing_no_offset(self):
        raw = load(TCL)
        raw["time_contract"] = "utc-with-offset"
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("beijing-no-offset", str(ctx.exception))
        path.unlink()

    def test_tcl_must_forbid_win009_power_commands(self):
        raw = load(TCL)
        raw["control"]["forbidden_commands"] = ["uut-reset"]
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("forbid", str(ctx.exception))
        path.unlink()

    def test_using_a_forbidden_command_is_refused(self):
        raw = load(TCL)
        raw["control"]["commands"]["power_cut"] = "uut-reset"
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("forbidden", str(ctx.exception))
        path.unlink()

    def test_profile_inheritance_is_refused(self):
        raw = load(MIDEA)
        raw["control"]["inherits"] = "tcl-pvm1005.json"
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("inherit", str(ctx.exception))
        path.unlink()

    def test_measured_profile_needs_com_port(self):
        raw = load(TCL)
        raw["chips"][0]["transport"]["log_port"] = None
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("log_port", str(ctx.exception))
        path.unlink()

    def test_unimplemented_driver_cannot_be_measured(self):
        raw = load(TCL)
        raw["chips"][0]["burn"]["driver"] = "csk6-batch"
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("not implemented", str(ctx.exception))
        path.unlink()


class MediaIsolationTests(unittest.TestCase):
    def test_media_profile_loads_as_draft(self):
        profile = Profile(MIDEA)
        self.assertEqual(profile.family, "midea")
        self.assertEqual(profile.status, "draft")
        self.assertFalse(profile.measured)
        self.assertTrue(profile.raw["measurement_required"])

    def test_media_does_not_inherit_tcl_control_or_ports(self):
        tcl = load(TCL)
        midea = load(MIDEA)
        tcl_values = set(strings(tcl))
        forbidden = {"uut-switch1.on", "uut-switch1.off", "uut-csk-boot.on", "uut-csk-boot.off",
                     "COM6", "COM3", "COM4", "AT+FTM=0", "921600", "3000000", 115200}
        overlap = {value for value in strings(midea) if value in forbidden and value in tcl_values}
        # 115200 may legitimately appear as a *comment* string; everything else must be TCL-free.
        self.assertEqual(overlap - {115200}, set())
        self.assertNotEqual(midea["control"]["kind"], tcl["control"]["kind"])

    def test_media_has_no_uut_commands(self):
        midea = load(MIDEA)
        self.assertEqual([value for value in strings(midea["control"]["commands"]) if value.startswith("uut-")], [])

    def test_media_recover_is_not_the_tcl_ftm_step(self):
        midea = load(MIDEA)
        self.assertNotEqual(midea["chips"][0]["recover"].get("driver"), "ftm-clear")
        self.assertIsNone(midea["chips"][0]["verify"].get("expect"))

    def test_non_tcl_uut_commands_need_explicit_declaration(self):
        raw = load(MIDEA)
        raw["control"].update({
            "kind": "serial-relay", "port": "COM6", "baud": 115200,
            "commands": {"power_cut": "uut-switch1.on", "power_restore": "uut-switch1.off"},
        })
        path = write_variant(raw)
        with self.assertRaises(ProfileError) as ctx:
            Profile(path)
        self.assertIn("allow_tcl_commands", str(ctx.exception))
        raw["control"]["allow_tcl_commands"] = True
        path.write_text(json.dumps(raw), encoding="utf-8")
        profile = Profile(path)
        self.assertTrue(profile.control["allow_tcl_commands"])
        path.unlink()


class PlanTests(unittest.TestCase):
    def test_tcl_plan_order(self):
        profile = Profile(TCL)
        self.assertEqual(plan_steps(profile), [
            "preflight", "boot:venusa", "burn:venusa", "verify:venusa",
            "burn:hisi", "recover:hisi", "verify:hisi", "receipt",
        ])

    def test_media_plan_skips_boot_and_recover(self):
        profile = Profile(MIDEA)
        self.assertEqual(plan_steps(profile), ["preflight", "burn:csk6", "verify:csk6", "receipt"])


if __name__ == "__main__":
    unittest.main()
