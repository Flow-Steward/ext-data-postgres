"""Reject the superseded Google API before native payloads can be packaged."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("google_api_floor", ROOT / "scripts/native_security.py")
assert SPEC is not None and SPEC.loader is not None
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)


def test_google_api_superseded_release_is_rejected():
    with pytest.raises(ValueError, match="below.*(?:security|baseline)|below the security"):
        NATIVE.check_floors(
            {"go_version": "go1.27.4", "modules": {"google.golang.org/api": "v0.314.0"}, "module_gaps": []},
            json.loads((ROOT / "scripts/native-security-baseline.json").read_text()),
        )
