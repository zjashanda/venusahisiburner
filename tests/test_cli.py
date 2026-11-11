"""CLI, state-machine and bench-lock tests. No serial port is opened."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import burn as cli  # noqa: E402
from core import bench as bench_module  # noqa: E402
from core import phases as phases_module  # noqa: E402
from core.contract import is_bj_timestamp  # noqa: E402
from core.profile import Profile  # noqa: E402
from core.state import RunState, StateError, plan_steps  # noqa: E402

TCL = ROOT / "scripts" / "profiles" / "tcl-pvm1005.json"
MIDEA = ROOT / "scripts" / "profiles" / "midea-csk6.json"


def no_serial():
    raise AssertionError("this path must not touch serial")


class DryRunTests(unittest.TestCase):
    def test_dry_run_never_opens_a_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bench_module, "serial_module", no_serial), \
                    mock.patch.object(phases_module, "serial_module", no_serial):
                code = cli.main(["all", "--profile", str(TCL), "--run-dir", tmp, "--dry-run"])
            leftovers = list(Path(tmp).glob("**/*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(leftovers, [])

    def test_guard_requires_confirm_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["all", "--profile", str(TCL), "--run-dir", tmp]), 3)
            self.assertEqual(cli.main(["step", "burn:venusa", "--profile", str(TCL), "--run-dir", tmp]), 3)

    def test_draft_profile_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["all", "--profile", str(MIDEA), "--run-dir", tmp, "--confirm-write"])
        self.assertEqual(code, 3)

    def test_draft_preflight_is_blocked_without_touching_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(phases_module, "serial_module", no_serial):
                code = cli.main(["preflight", "--profile", str(MIDEA), "--run-dir", tmp])
            receipt = json.loads((Path(tmp) / "steps" / "preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 4)
        self.assertEqual(receipt["verdict"], "BLOCKED")
        self.assertTrue(receipt["measurement_required"])

    def test_receipt_before_steps_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["receipt", "--profile", str(TCL), "--run-dir", tmp])
        self.assertEqual(code, 2)


class StateMachineTests(unittest.TestCase):
    def plan(self):
        return plan_steps(Profile(TCL))

    def test_out_of_order_step_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState(Path(tmp))
            with self.assertRaises(StateError):
                state.require("verify:venusa", self.plan())
            state.complete("preflight", "PASS")
            state.require("boot:venusa", self.plan())
            with self.assertRaises(StateError):
                state.require("burn:hisi", self.plan())

    def test_failed_step_is_never_marked_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState(Path(tmp))
            state.complete("burn:venusa", "FAIL")
            self.assertFalse(state.done("burn:venusa"))
            state.fail("burn:venusa", "FAIL", "flash marker missing")
            self.assertEqual(state.data["failed"][-1]["reason"], "flash marker missing")

    def test_completion_persists_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState(Path(tmp))
            state.complete("preflight", "PASS")
            again = RunState(Path(tmp))
            self.assertTrue(again.done("preflight"))

    def test_step_receipts_use_the_beijing_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = phases_module._step_receipt({"run_dir": tmp}, "burn:venusa", {"verdict": "PASS"})
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(is_bj_timestamp(payload["at"]))
        self.assertEqual(payload["step"], "burn:venusa")

    def test_receipt_hash_list_excludes_mutable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "steps").mkdir()
            (root / "steps" / "preflight.json").write_text("{}", encoding="utf-8")
            (root / "state.json").write_text("{}", encoding="utf-8")
            (root / "run.receipt.json").write_text("{}", encoding="utf-8")
            hashed = phases_module._artifacts(root)
        self.assertIn("steps/preflight.json", hashed)
        self.assertNotIn("state.json", hashed)
        self.assertNotIn("run.receipt.json", hashed)


class BenchLockTests(unittest.TestCase):
    def lock_path(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return Path(tmp) / "bench.lock"

    def test_second_holder_is_refused_not_killed(self):
        path = self.lock_path()
        with bench_module.BenchLock("pvm1005", path):
            with self.assertRaises(bench_module.BenchError) as ctx:
                with bench_module.BenchLock("pvm1005", path):
                    pass
        self.assertIn("locked", str(ctx.exception))
        self.assertIn("不抢占", str(ctx.exception))

    def test_lock_is_released_after_use(self):
        path = self.lock_path()
        with bench_module.BenchLock("pvm1005", path):
            self.assertTrue(path.is_file())
        self.assertFalse(path.exists())

    def test_stale_lock_from_dead_pid_is_taken_over(self):
        path = self.lock_path()
        path.write_text(json.dumps({"pid": 999999, "since": "2026-11-10 10:00:00"}), encoding="utf-8")
        with bench_module.BenchLock("pvm1005", path) as lock:
            self.assertTrue(lock.held)
            self.assertIn(str(os.getpid()), path.read_text(encoding="utf-8"))

    def test_pid_liveness(self):
        self.assertTrue(bench_module._pid_alive(os.getpid()))
        self.assertFalse(bench_module._pid_alive(999999))


if __name__ == "__main__":
    unittest.main()
