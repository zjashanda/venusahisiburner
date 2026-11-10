#!/usr/bin/env python3
"""Local skill-git lint/scan gate for venusahisiburner.

Checks (no hardware, no network):
  1. SKILL.md frontmatter: name/description present, name matches the repo dir.
  2. every JSON file parses; profiles and the asset manifest use the right schema.
  3. profile asset ids all exist in assets/manifest.json.
  4. secret scan over shipped files (SKILL.md, references/, assets/, scripts/profiles/):
     literal tokens/keys/passwords fail unless the line is marked ``secret-scan: allow``.
  5. python compileall of scripts/ and tests/.

Usage: python scripts/lint_scan.py [--strict]
Exit: 0 clean, 1 findings (when --strict), 2 usage.
"""
from __future__ import annotations

import argparse
import compileall
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from core.assets import Assets  # noqa: E402
from core.profile import Profile  # noqa: E402

SECRET = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|x-access-token:|password\s*=|passwd\s*=)", re.I)  # secret-scan: allow
ALLOW = "secret-scan: allow"
SCAN_GLOBS = ["SKILL.md", "references/*.md", "assets/*.json", "scripts/profiles/*.json",
              "scripts/*.py", "scripts/core/*.py"]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


def files(pattern: str) -> list[Path]:
    return [path for path in sorted(ROOT.glob(pattern)) if path.is_file()]


def check_frontmatter(findings: list[str]) -> dict:
    path = ROOT / "SKILL.md"
    if not path.is_file():
        findings.append("SKILL.md missing")
        return {}
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        findings.append("SKILL.md has no YAML frontmatter")
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    for key in ("name", "description"):
        if not meta.get(key):
            findings.append(f"SKILL.md frontmatter missing {key}")
    if meta.get("name") and meta["name"] != ROOT.name:
        findings.append(f"frontmatter name {meta['name']!r} != repo dir {ROOT.name!r}")
    return meta


def check_json(findings: list[str]) -> None:
    for path in ROOT.rglob("*.json"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            findings.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")


def check_profiles(findings: list[str], manifest: Path) -> None:
    assets = Assets(manifest)
    for path in files("scripts/profiles/*.json"):
        try:
            profile = Profile(path)
        except Exception as exc:  # noqa: BLE001 - every load failure is a finding
            findings.append(f"{path.name}: {exc}")
            continue
        try:
            assets.require(profile.asset_ids())
        except Exception as exc:  # noqa: BLE001
            findings.append(f"{path.name}: {exc}")


def check_secrets(findings: list[str]) -> None:
    for pattern in SCAN_GLOBS:
        for path in files(pattern):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if SECRET.search(line) and ALLOW not in line:
                    findings.append(f"{path.relative_to(ROOT)}:{number}: possible literal secret")


def check_compile(findings: list[str]) -> None:
    for directory in ("scripts", "tests"):
        if not compileall.compile_dir(str(ROOT / directory), quiet=2, force=True):
            findings.append(f"{directory}/: compileall failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="venusahisiburner lint/scan")
    parser.add_argument("--strict", action="store_true", help="exit 1 on findings")
    args = parser.parse_args(argv)

    findings: list[str] = []
    meta = check_frontmatter(findings)
    check_json(findings)
    manifest = ROOT / "assets" / "manifest.json"
    if manifest.is_file():
        check_profiles(findings, manifest)
    else:
        findings.append("assets/manifest.json missing")
    check_secrets(findings)
    check_compile(findings)

    report = {
        "skill": meta.get("name"),
        "root": str(ROOT),
        "checks": ["frontmatter", "json", "profiles", "secrets", "compileall"],
        "findings": findings,
        "verdict": "PASS" if not findings else "FAIL",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if findings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
