"""This bundle's vendored wheels are pinned the way an offline install needs.

`flow-steward extensions test` installs with `--no-index --find-links dev-wheels`
and is the proof that the pins resolve; there is no need to restate it here, and
no need to demand that a dependency be platform-specific at all.

What is left is the one thing a filename cannot say for itself: a wheel pinned by
path for one platform must carry the marker that keeps pip from trying it on the
others. Everything is derived from this bundle's own files, so adding or
upgrading a dependency needs no edit here.
"""

from __future__ import annotations

import re
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = BUNDLE_ROOT / "requirements-dev.txt"

#: How a wheel filename names a platform, and the marker a pin for it must carry.
PLATFORM_MARKERS = {
    re.compile(r"macosx_\d+_\d+_arm64"): (
        'sys_platform == "darwin"',
        'platform_machine == "arm64"',
    ),
    re.compile(r"manylinux[^-]*aarch64"): (
        'sys_platform == "linux"',
        'platform_machine == "aarch64"',
    ),
    re.compile(r"manylinux[^-]*x86_64"): (
        'sys_platform == "linux"',
        'platform_machine == "x86_64"',
    ),
}


def test_a_pinned_platform_specific_wheel_carries_a_matching_marker() -> None:
    """Without a marker pip would try one architecture's wheel on all of them."""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "dev-wheels/" not in line:
            continue
        wheel_name = line.split(";")[0].strip().rsplit("/", 1)[-1]
        marker = line.split(";", 1)[1] if ";" in line else ""
        for pattern, fragments in PLATFORM_MARKERS.items():
            if not pattern.search(wheel_name):
                continue
            for fragment in fragments:
                assert fragment in marker, f"{wheel_name} is missing marker {fragment}"
