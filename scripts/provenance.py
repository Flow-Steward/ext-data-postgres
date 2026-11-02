#!/usr/bin/env python3
"""Record pinned native build provenance and per-target SHA-256 values."""

from pathlib import Path
import hashlib
import json

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
        }
        for target in TARGETS
    }
    data = {
        "upstream_commit": "b0010060a939455d91e4f8ba62dc3e72b1892469",
        "upstream_tag": "v1.3.0",
        "source": "https://github.com/googleapis/mcp-toolbox",
        "go_image": "golang:1.26.2-bookworm@sha256:47ce5636e9936b2c5cbf708925578ef386b4f8872aec74a67bd13a627d242b19",
        "registered_prebuilts": (ROOT / "scripts/toolbox-prebuilts.txt").read_text().split(),
        "patch_sha256": digest(
            ROOT / "scripts/patches/0001-flowsteward-driver-bound-query-params.patch"
        ),
        "binaries": records,
    }
    (ROOT / "toolbox-provenance.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    (ROOT / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['path']}\n" for record in records.values())
    )


if __name__ == "__main__":
    main()
