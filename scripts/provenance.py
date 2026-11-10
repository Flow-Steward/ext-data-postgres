#!/usr/bin/env python3
"""Record pinned native build provenance and per-target SHA-256 values."""

from pathlib import Path
import hashlib
import json
from native_security import inspect_binary, check_floors

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("linux-amd64", "linux-arm64")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    records = {
        target: {
            "path": f"bin/{target}/toolbox",
            "sha256": digest(ROOT / f"bin/{target}/toolbox"),
            "size": (ROOT / f"bin/{target}/toolbox").stat().st_size,
            **inspect_binary(ROOT / f"bin/{target}/toolbox"),
        }
        for target in TARGETS
    }
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    for record in records.values():
        check_floors(record, baseline)
    data = {
        "upstream_commit": "06ea49d9a487a72c22419b5a8d5c22c11f0d7485",
        "upstream_tag": "v1.16.0",
        "source": "https://github.com/googleapis/mcp-toolbox",
        "go_image": "golang:1.27.3-bookworm@sha256:dc0963f32053e4bc451d24d87e3892d42d01021b61f093e98eb95c0352bb84b0",
        "registered_prebuilts": (ROOT / "scripts/toolbox-prebuilts.txt").read_text().split(),
        "patch_sha256": digest(
            ROOT / "scripts/patches/0001-flowsteward-driver-bound-query-params.patch"
        ),
        "registered_packages": (ROOT / "packages.txt").read_text().splitlines(),
        "managed_vector_mode": True,
        "security_baseline": baseline,
        "build_identities": json.loads((ROOT / "scripts/native-build-identities.json").read_text()),
        "approved_profiles": json.loads((ROOT / "scripts/approved-profile-baseline.json").read_text()),
        "patches": {path.relative_to(ROOT).as_posix(): digest(path) for path in sorted((ROOT / "scripts/patches").glob("*.patch"))},
        "binaries": records,
    }
    (ROOT / "toolbox-provenance.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    (ROOT / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['path']}\n" for record in records.values())
    )


if __name__ == "__main__":
    main()
