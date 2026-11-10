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
        "upstream_commit": "b0010060a939455d91e4f8ba62dc3e72b1892469",
        "upstream_tag": "v1.3.0",
        "source": "https://github.com/googleapis/mcp-toolbox",
        "go_image": "golang:1.26.10-bookworm@sha256:5de7c99eb78ca5e515a4c340235d0955ec5964a5071c492c15503d7c267da473",
        "registered_prebuilts": (ROOT / "scripts/toolbox-prebuilts.txt").read_text().split(),
        "patch_sha256": digest(
            ROOT / "scripts/patches/0001-flowsteward-driver-bound-query-params.patch"
        ),
        "registered_packages": (ROOT / "packages.txt").read_text().splitlines(),
        "managed_vector_mode": True,
        "security_baseline": baseline,
        "patches": {path.relative_to(ROOT).as_posix(): digest(path) for path in sorted((ROOT / "scripts/patches").glob("*.patch"))},
        "binaries": records,
    }
    (ROOT / "toolbox-provenance.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    (ROOT / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['path']}\n" for record in records.values())
    )


if __name__ == "__main__":
    main()
