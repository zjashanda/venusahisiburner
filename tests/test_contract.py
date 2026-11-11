"""Contract tests: Beijing no-offset time, hashes, version and ASR parsing."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from core.contract import (classify_asr, is_bj_timestamp, now_bj, parse_bj,  # noqa: E402
                           parse_versions, sha256_file, sha256_text)


class TimeContractTests(unittest.TestCase):
    def test_now_bj_has_no_offset(self):
        value = now_bj()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("+08:00", value)
        self.assertNotIn("Z", value)
        self.assertNotIn("T", value)

    def test_now_bj_is_beijing_wall_clock(self):
        expected = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        actual = parse_bj(now_bj()).replace(tzinfo=None)
        self.assertLess(abs((expected - actual).total_seconds()), 5)

    def test_forbidden_shapes_are_rejected(self):
# time-lint:allow negative fixtures: T separator / Z / offset / fractional seconds
        for bad in ("2026-11-10T14:05:00.123+08:00", "2026-11-10T06:05:00.123Z",  # time-lint:allow
                    "2026-11-10 14:05:00.123", "2026-11-10T14:05:00",  # time-lint:allow
                    "2026-11-10 14:05:00+08:00"):  # time-lint:allow
            self.assertFalse(is_bj_timestamp(bad), bad)

    def test_valid_timestamp_round_trips(self):
        value = "2026-11-10 14:05:00"
        self.assertTrue(is_bj_timestamp(value))
        self.assertEqual(parse_bj(value).strftime("%Y-%m-%d %H:%M:%S"), value)

    def test_lexical_order_matches_time_order(self):
        earlier = "2026-11-10 09:05:00"
        later = "2026-11-10 14:05:00"
        self.assertLess(earlier, later)


class HashTests(unittest.TestCase):
    def test_sha256_of_known_bytes(self):
        self.assertEqual(sha256_text("abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_sha256_file_matches_text(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abc.bin"
            path.write_bytes(b"abc")
            self.assertEqual(sha256_file(path), sha256_text("abc"))


class VersionParsingTests(unittest.TestCase):
    def test_versions_from_real_boot_lines(self):
        lines = [
            "[1970-01-01 08:00:00.033][O][boot # project_flash] ======== FLASH BOOT LOCK VERSION: 1.1.1 ========",  # time-lint:allow
            "boot version: 1.0.4 boot_876a194e_2026-06-16_10:37:38",
            "Version         :asr_dsp/SP-LVM141-A-ChXx-FX-V0.0.41",
            "[0mLisa Player Version 1.2.3.1002_8543e2c",
            "[36;22m01-01 00:00:00.130-LT I/main   [eagle_ini](app_main.c:148) - SYS_VER: V8-H363T20-WFMV206.111.1.16",  # time-lint:allow
            "[36;22m01-01 00:00:00.129-LT I/main   [eagle_ini](app_main.c:147) - SDK_VER: tcl.iot.wifidev.sdk_v 2.0.1",  # time-lint:allow
        ]
        found = parse_versions(lines)
        self.assertEqual(found["csk_app"], "asr_dsp/SP-LVM141-A-ChXx-FX-V0.0.41")
        self.assertEqual(found["csk_boot"], "1.0.4")
        self.assertEqual(found["csk_player"], "1.2.3.1002_8543e2c")
        self.assertEqual(found["hisi_sys_ver"], "V8-H363T20-WFMV206.111.1.16")
        self.assertEqual(found["hisi_sdk_ver"], "tcl.iot.wifidev.sdk_v")

    def test_asr_markers(self):
        self.assertEqual(classify_asr(["use_fixed_asr:0"])["path"], "cloud")
        self.assertEqual(classify_asr(["use_fixed_asr:1"])["path"], "offline")
        self.assertEqual(classify_asr(["nothing"])["path"], "unknown")


if __name__ == "__main__":
    unittest.main()
