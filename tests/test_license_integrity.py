"""Keep the exact upstream legal license artifact unchanged."""

import hashlib
from pathlib import Path


def test_upstream_license_is_byte_exact():
    path = Path(__file__).resolve().parents[1] / "LICENSE"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"
    )
