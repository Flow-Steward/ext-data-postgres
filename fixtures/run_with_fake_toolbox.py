#!/usr/bin/env python3
"""Run the extension entrypoint with an explicitly injected stateless MCP double."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    spec = importlib.util.spec_from_file_location("toolbox_runtime_under_test", ROOT / "main.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Toolbox extension runtime")
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._TOOLBOX_COMMAND_OVERRIDE = [sys.executable, str(ROOT / "fixtures" / "fake_toolbox.py")]
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
