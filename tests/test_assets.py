"""Asset manifest tests: schema, secret boundary, hash verification, fetch seam."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from core import assets as assets_module  # noqa: E402
from core.assets import AssetError, Assets, FetchBlocked  # noqa: E402
from core.contract import sha256_text  # noqa: E402

MANIFEST = ROOT / "assets" / "manifest.json"


def base_manifest(tmp: Path, payload: bytes = b"tool-bytes") -> dict:
    sha = sha256_text(payload.decode())
    return {
        "schema": "venusahisiburner-assets/v1",
        "oss": {
            "provider": "ark-oss", "endpoint_env": "ARK_OSS_ENDPOINT", "bucket_env": "ARK_OSS_BUCKET",
            "credential": {"mode": "env-only", "access_key_env": "ARK_OSS_ACCESS_KEY_ID",
                           "secret_key_env": "ARK_OSS_ACCESS_KEY_SECRET", "persist": False},
        },
        "assets": [{
            "id": "demo-tool", "kind": "tool", "target_chip": "CSK7/VenusA", "version": "1.0.0",
            "file": "tool.bin", "bytes": len(payload), "sha256": sha,
            "oss": {"key": "tools/demo-tool/1.0.0/tool.bin", "object": "tool.bin", "download": "ossutil"},
            "install": {"material_rel": "demo/tool.bin", "cache_rel": "tools/demo/tool.bin"},
        }],
    }


class ManifestTests(unittest.TestCase):
    def test_repo_manifest_loads_and_declares_the_verified_baseline(self):
        assets = Assets(MANIFEST)
        for asset_id in ("csk-uart-burn-tool", "hisi-burntool-gold",
                         "tcl-csk7-v0.0.41", "hisi-ws63-v8-h363t20-wfmv206.111.1.16"):
            entry = assets.get(asset_id)
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertTrue(entry["oss"]["key"])
        self.assertEqual(assets.get("tcl-csk7-v0.0.41")["version"], "V0.0.41")
        self.assertEqual(assets.get("hisi-ws63-v8-h363t20-wfmv206.111.1.16")["version"],
                         "V8-H363T20-WFMV206.111.1.16")

    def test_literal_credential_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = base_manifest(Path(tmp))
            raw["oss"]["credential"]["access_key"] = "AKIAABCDEFGHIJKLMNOP"  # secret-scan: allow
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(AssetError) as ctx:
                Assets(path)
            self.assertIn("literal secret", str(ctx.exception))

    def test_literal_secret_value_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = base_manifest(Path(tmp))
            raw["assets"][0]["provenance"] = {"note": "token ghp_abcdefghijklmnopqrstuvwxyz012345"}  # secret-scan: allow
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(AssetError) as ctx:
                Assets(path)
            self.assertIn("literal secret", str(ctx.exception))

    def test_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = base_manifest(Path(tmp))
            raw["assets"].append(dict(raw["assets"][0]))
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(AssetError) as ctx:
                Assets(path)
            self.assertIn("duplicate", str(ctx.exception))


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = b"tool-bytes"
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(base_manifest(self.root, self.payload)), encoding="utf-8")
        self.assets = Assets(self.manifest)
        (self.root / "material" / "demo").mkdir(parents=True)
        (self.root / "material" / "demo" / "tool.bin").write_bytes(self.payload)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_passes_on_identical_bytes(self):
        report = self.assets.verify("demo-tool", self.root / "material", None)
        self.assertTrue(report["exists"])
        self.assertTrue(report["sha_ok"])
        self.assertTrue(report["size_ok"])

    def test_verify_fails_on_modified_bytes(self):
        (self.root / "material" / "demo" / "tool.bin").write_bytes(b"tool-bytez")
        report = self.assets.verify("demo-tool", self.root / "material", None)
        self.assertFalse(report["sha_ok"])
        self.assertFalse(self.assets.verify_all(["demo-tool"], self.root / "material", None)["all_ok"])

    def test_cache_wins_over_material_tree(self):
        (self.root / "cache" / "tools" / "demo").mkdir(parents=True)
        (self.root / "cache" / "tools" / "demo" / "tool.bin").write_bytes(self.payload)
        path = self.assets.resolve("demo-tool", self.root / "material", self.root / "cache")
        self.assertIn("cache", str(path))

    def test_missing_asset_reports_the_searched_paths(self):
        report = self.assets.verify("demo-tool", self.root / "nowhere", None)
        self.assertFalse(report["exists"])
        self.assertIn("not found locally", report["reason"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = b"tool-bytes"
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(base_manifest(self.root, self.payload)), encoding="utf-8")
        self.assets = Assets(self.manifest)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_bucket_env_blocks_the_download(self):
        with self.assertRaises(FetchBlocked) as ctx:
            self.assets.fetch("demo-tool", self.root / "out.bin", env={})
        self.assertIn("ARK_OSS_BUCKET", str(ctx.exception))

    def test_missing_ossutil_blocks_the_download(self):
        with mock.patch.object(assets_module.shutil, "which", return_value=None):
            with self.assertRaises(FetchBlocked) as ctx:
                self.assets.fetch("demo-tool", self.root / "out.bin", env={"ARK_OSS_BUCKET": "ark-tools"})
        self.assertIn("ossutil", str(ctx.exception))

    def test_download_is_hash_verified(self):
        dest = self.root / "cache" / "out.bin"

        def fake_run(argv, **kwargs):
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(argv[-1]).write_bytes(self.payload)

            class Result:
                returncode = 0
                stderr = ""

            return Result()

        with mock.patch.object(assets_module.shutil, "which", return_value="ossutil"), \
                mock.patch.object(assets_module.subprocess, "run", side_effect=fake_run):
            report = self.assets.fetch("demo-tool", dest, env={"ARK_OSS_BUCKET": "ark-tools"})
        self.assertTrue(report["ok"])
        self.assertEqual(dest.read_bytes(), self.payload)

    def test_bad_download_is_removed(self):
        dest = self.root / "cache" / "out.bin"

        def fake_run(argv, **kwargs):
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(argv[-1]).write_bytes(b"wrong")

            class Result:
                returncode = 0
                stderr = ""

            return Result()

        with mock.patch.object(assets_module.shutil, "which", return_value="ossutil"), \
                mock.patch.object(assets_module.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(AssetError):
                self.assets.fetch("demo-tool", dest, env={"ARK_OSS_BUCKET": "ark-tools"})
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
