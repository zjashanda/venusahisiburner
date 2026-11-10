"""Unified tool/firmware asset manifest (OSS coordinates + local resolution).

One manifest for every project, so burn tools are never copied into a
project skill:

  * OSS coordinates: key, object name, version, target chip, SHA-256,
    download method and the credential boundary (env variable NAMES only).
  * Local resolution: a node-level tool cache (``--asset-cache``) or the
    run's material tree (``--material-dir``); the SHA-256 decides whether a
    local copy is the declared asset.
  * No secret ever lives here: literal keys/tokens/passwords are rejected at
    load time, and the credential block may only name environment variables.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from .contract import SCHEMAS, is_sha256, sha256_file

DOWNLOADS = {"ossutil", "storage-http", "local-only"}
KINDS = {"tool", "firmware"}
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
SENSITIVE_KEY = re.compile(r"(secret|passw|token|access_key|private_key|credential|ak_|sk_)", re.I)
SECRET_VALUE = re.compile(r"(ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|x-access-token:|password\s*=)", re.I)  # secret-scan: allow


class AssetError(RuntimeError):
    """Manifest is invalid, an asset is missing, or a local copy is wrong."""


class FetchBlocked(AssetError):
    """A download was requested but the credential boundary is not satisfied."""


def _walk_leaves(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                out.extend(_walk_leaves(value, child))
            else:
                out.append((child, value))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(_walk_leaves(value, f"{path}[{index}]"))
    return out


class Assets:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise AssetError(f"asset manifest not found: {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AssetError(f"asset manifest is not valid JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise AssetError("asset manifest root must be an object")
        self.raw = raw
        self._validate(raw)

    def _validate(self, raw: dict) -> None:
        if raw.get("schema") != SCHEMAS["assets"]:
            raise AssetError(f"manifest schema must be {SCHEMAS['assets']}, got {raw.get('schema')!r}")
        oss = raw.get("oss")
        if not isinstance(oss, dict):
            raise AssetError("manifest.oss is required")
        for key in ("provider", "endpoint_env", "bucket_env"):
            value = oss.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AssetError(f"manifest.oss.{key} is required")
        for key in ("endpoint_env", "bucket_env"):
            if not ENV_NAME.match(oss[key]):
                raise AssetError(f"manifest.oss.{key} must be an ENV VAR NAME, got {oss[key]!r}")
        credential = oss.get("credential")
        if not isinstance(credential, dict):
            raise AssetError("manifest.oss.credential is required")
        if credential.get("mode") != "env-only":
            raise AssetError("manifest.oss.credential.mode must be 'env-only'")
        if credential.get("persist") is not False:
            raise AssetError("manifest.oss.credential.persist must be false (never write credentials to disk)")
        for key in ("access_key_env", "secret_key_env"):
            value = credential.get(key)
            if not isinstance(value, str) or not ENV_NAME.match(value):
                raise AssetError(f"manifest.oss.credential.{key} must be an ENV VAR NAME, got {value!r}")
        self.oss = oss
        self.bucket_env = oss["bucket_env"]
        self.endpoint_env = oss["endpoint_env"]

        assets = raw.get("assets")
        if not isinstance(assets, list) or not assets:
            raise AssetError("manifest.assets must be a non-empty list")
        seen: set[str] = set()
        for index, asset in enumerate(assets):
            where = f"assets[{index}]"
            if not isinstance(asset, dict):
                raise AssetError(f"{where} must be an object")
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise AssetError(f"{where}.id is required")
            if asset_id in seen:
                raise AssetError(f"duplicate asset id {asset_id!r}")
            seen.add(asset_id)
            if asset.get("kind") not in KINDS:
                raise AssetError(f"{where}.kind must be one of {sorted(KINDS)}")
            for key in ("target_chip", "version", "file"):
                if not str(asset.get(key) or "").strip():
                    raise AssetError(f"{where}.{key} is required")
            if not is_sha256(asset.get("sha256")):
                raise AssetError(f"{where}.sha256 must be 64 lowercase hex chars")
            if not isinstance(asset.get("bytes"), int) or asset["bytes"] <= 0:
                raise AssetError(f"{where}.bytes must be a positive int")
            oss_block = asset.get("oss")
            if not isinstance(oss_block, dict):
                raise AssetError(f"{where}.oss is required")
            for key in ("key", "object"):
                if not str(oss_block.get(key) or "").strip():
                    raise AssetError(f"{where}.oss.{key} is required")
            if str(oss_block["key"]).startswith("/"):
                raise AssetError(f"{where}.oss.key must not start with '/'")
            if oss_block.get("download") not in DOWNLOADS:
                raise AssetError(f"{where}.oss.download must be one of {sorted(DOWNLOADS)}")
            install = asset.get("install")
            if not isinstance(install, dict) or not (install.get("material_rel") or install.get("cache_rel")):
                raise AssetError(f"{where}.install needs material_rel and/or cache_rel")
        self.assets = assets
        self.by_id = {asset["id"]: asset for asset in assets}
        self._reject_secrets(raw)

    def _reject_secrets(self, raw: dict) -> None:
        for path, value in _walk_leaves(raw):
            if not isinstance(value, str):
                continue
            if SECRET_VALUE.search(value):
                raise AssetError(f"manifest contains a literal secret at {path}; keep credentials in env vars")
            leaf = path.rsplit(".", 1)[-1]
            if SENSITIVE_KEY.search(leaf) and not leaf.endswith("_env") and leaf != "mode":
                if not ENV_NAME.match(value):
                    raise AssetError(
                        f"manifest.{path} looks like a literal credential ({value!r}); "
                        "declare the environment variable NAME instead")

    # ------------------------------------------------------------ queries
    def require(self, ids: list[str]) -> None:
        missing = [asset_id for asset_id in ids if asset_id not in self.by_id]
        if missing:
            raise AssetError(f"profile references asset ids not present in {self.path.name}: {missing}")

    def get(self, asset_id: str) -> dict:
        if asset_id not in self.by_id:
            raise AssetError(f"unknown asset id {asset_id!r}")
        return self.by_id[asset_id]

    def describe(self, asset_id: str) -> dict[str, Any]:
        asset = self.get(asset_id)
        return {
            "id": asset["id"], "kind": asset["kind"], "target_chip": asset["target_chip"],
            "version": asset["version"], "file": asset["file"], "bytes": asset["bytes"],
            "sha256": asset["sha256"], "oss": dict(asset["oss"]),
            "install": dict(asset["install"]), "provenance": asset.get("provenance"),
        }

    # ------------------------------------------------------------ local files
    def resolve(self, asset_id: str, material_dir: Path | None, cache_dir: Path | None) -> Path:
        asset = self.get(asset_id)
        install = asset["install"]
        candidates: list[Path] = []
        if cache_dir and install.get("cache_rel"):
            candidates.append(Path(cache_dir) / install["cache_rel"])
        if material_dir and install.get("material_rel"):
            candidates.append(Path(material_dir) / install["material_rel"])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(candidate) for candidate in candidates) or "<no local source configured>"
        raise AssetError(
            f"asset {asset_id} ({asset['file']}) not found locally; searched {searched}. "
            f"是补齐 {install.get('material_rel') or install.get('cache_rel')}，还是用 --fetch-assets 从 OSS 拉取?")

    def verify(self, asset_id: str, material_dir: Path | None, cache_dir: Path | None) -> dict[str, Any]:
        asset = self.get(asset_id)
        report: dict[str, Any] = {
            "id": asset_id, "file": asset["file"], "target_chip": asset["target_chip"],
            "version": asset["version"], "expected_sha256": asset["sha256"],
            "expected_bytes": asset["bytes"],
        }
        try:
            path = self.resolve(asset_id, material_dir, cache_dir)
        except AssetError as exc:
            report.update({"exists": False, "sha_ok": False, "size_ok": False, "reason": str(exc)})
            return report
        actual_sha = sha256_file(path)
        actual_bytes = path.stat().st_size
        report.update({
            "exists": True, "path": str(path), "sha256": actual_sha, "bytes": actual_bytes,
            "sha_ok": actual_sha == asset["sha256"], "size_ok": actual_bytes == asset["bytes"],
        })
        return report

    def verify_all(self, ids: list[str], material_dir: Path | None, cache_dir: Path | None) -> dict[str, Any]:
        reports = {asset_id: self.verify(asset_id, material_dir, cache_dir) for asset_id in ids}
        return {
            "assets": reports,
            "all_ok": all(item["sha_ok"] and item["size_ok"] for item in reports.values()),
        }

    def fetch(self, asset_id: str, dest: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
        """Download one asset through the declared method, then verify it.

        The credential boundary is env-only: without the bucket variable (or
        without the tool on PATH) the call is BLOCKED, never retried with a
        guessed default.
        """
        asset = self.get(asset_id)
        env = dict(os.environ if env is None else env)
        method = asset["oss"]["download"]
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if method == "local-only":
            raise FetchBlocked(f"asset {asset_id} is declared local-only; provision it on the node cache")
        if method == "ossutil":
            bucket = env.get(self.bucket_env, "").strip()
            if not bucket:
                raise FetchBlocked(f"{self.bucket_env} is not set; refusing to guess an OSS bucket")
            binary = shutil.which("ossutil") or shutil.which("ossutil64")
            if not binary:
                raise FetchBlocked("ossutil is not on PATH; cannot download from OSS")
            uri = f"oss://{bucket}/{asset['oss']['key']}"
            proc = subprocess.run([binary, "cp", "-f", uri, str(dest)], capture_output=True, text=True)
            if proc.returncode != 0:
                raise AssetError(f"ossutil failed for {asset_id}: {proc.stderr.strip()[:400]}")
        elif method == "storage-http":
            base = env.get(self.endpoint_env, "").strip().rstrip("/")
            if not base:
                raise FetchBlocked(f"{self.endpoint_env} is not set; refusing to guess a download endpoint")
            url = f"{base}/{asset['oss']['key']}"
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - scheme from env config
                dest.write_bytes(response.read())
        else:  # pragma: no cover - guarded by manifest validation
            raise FetchBlocked(f"unknown download method {method!r}")

        actual = sha256_file(dest)
        ok = actual == asset["sha256"] and dest.stat().st_size == asset["bytes"]
        report = {"id": asset_id, "method": method, "path": str(dest), "sha256": actual,
                  "expected_sha256": asset["sha256"], "sha_ok": actual == asset["sha256"],
                  "bytes": dest.stat().st_size, "ok": ok}
        if not ok:
            dest.unlink(missing_ok=True)
            raise AssetError(f"downloaded asset {asset_id} failed sha256/size verification; file removed")
        return report
