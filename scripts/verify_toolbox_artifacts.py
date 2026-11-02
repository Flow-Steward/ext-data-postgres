#!/usr/bin/env python3
"""Verify packaged Toolbox binaries without downloading or executing them."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

MAX_BINARY_BYTES = 150 * 1024 * 1024
EXPECTED_MACHINES = {
    "bin/linux-amd64/toolbox": 62,
    "bin/linux-arm64/toolbox": 183,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--checksums", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = args.root.resolve()
    checksum_path = (args.checksums or root / "SHA256SUMS").resolve()
    failures: list[str] = []
    rows: dict[str, str] = {}
    package_target = ""
    package_metadata_path = root / ".fs-package.yaml"
    if package_metadata_path.is_file():
        try:
            import yaml

            package_metadata = yaml.safe_load(package_metadata_path.read_text(encoding="utf-8"))
        except (ImportError, OSError) as exc:
            print(f"[FAIL] cannot read package metadata: {exc}", file=sys.stderr)
            return 1
        except yaml.YAMLError as exc:
            print(f"[FAIL] cannot read package metadata: {exc}", file=sys.stderr)
            return 1
        if not isinstance(package_metadata, dict):
            print("[FAIL] package metadata must be an object", file=sys.stderr)
            return 1
        package_target = str(package_metadata.get("runtime_target") or "").strip()
        if package_target not in {"linux-amd64", "linux-arm64"}:
            print(f"[FAIL] unsupported runtime target: {package_target!r}", file=sys.stderr)
            return 1
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        print(f"[FAIL] cannot read checksums: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        try:
            checksum, relative = line.split("  ", 1)
        except ValueError:
            failures.append(f"malformed checksum row: {line!r}")
            continue
        rows[relative] = checksum
    unexpected_rows = set(rows) - set(EXPECTED_MACHINES)
    if unexpected_rows:
        failures.extend(
            f"unexpected artifact path: {relative}" for relative in sorted(unexpected_rows)
        )
    if package_target:
        target_relative = f"bin/{package_target}/toolbox"
        present_artifacts = {
            path.relative_to(root).as_posix()
            for path in (root / "bin").rglob("*")
            if path.is_file()
        }
        if present_artifacts != {target_relative}:
            failures.append(
                f"target package must contain exactly {target_relative}; found {sorted(present_artifacts)}"
            )
        if target_relative not in rows:
            failures.append(f"checksum missing: {target_relative}")
        active_rows = {target_relative: rows[target_relative]} if target_relative in rows else {}
    else:
        if set(rows) != set(EXPECTED_MACHINES):
            failures.append("SHA256SUMS must cover exactly linux-amd64 and linux-arm64")
        active_rows = rows
    for relative, checksum in active_rows.items():
        expected_machine = EXPECTED_MACHINES.get(relative)
        if expected_machine is None:
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(f"artifact escapes extension root: {relative}")
            continue
        if not path.is_file():
            failures.append(f"artifact missing: {relative}")
            continue
        header = path.read_bytes()[:20]
        if header[:4] != b"\x7fELF" or int.from_bytes(header[18:20], "little") != expected_machine:
            failures.append(f"unexpected ELF architecture: {relative}")
        if not os.access(path, os.X_OK):
            failures.append(f"artifact is not executable: {relative}")
        if path.stat().st_size >= MAX_BINARY_BYTES:
            failures.append(f"artifact exceeds {MAX_BINARY_BYTES} bytes: {relative}")
        if _sha256(path) != checksum:
            failures.append(f"checksum mismatch: {relative}")
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}", file=sys.stderr)
        return 1
    for relative in sorted(active_rows):
        path = root / relative
        print(f"[PASS] {relative} bytes={path.stat().st_size} sha256={active_rows[relative]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
