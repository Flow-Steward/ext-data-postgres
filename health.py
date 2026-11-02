#!/usr/bin/env python3
"""Health check for the PostgreSQL extension."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _supported_connector_ids() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "toolbox_dataset_connector_main", Path(__file__).resolve().with_name("main.py")
    )
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [row["connector_type_id"] for row in module._supported_connector_rows()]


def main() -> int:
    spec = importlib.util.spec_from_file_location(
        "toolbox_dataset_connector_runtime", Path(__file__).resolve().with_name("main.py")
    )
    if spec is None or spec.loader is None:
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module._toolbox_command({"prebuilt": "postgres"})
    except ValueError as exc:
        print(json.dumps({"ok": False, "status": "unhealthy", "detail": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "status": "healthy",
                "extension_id": "flowsteward.data-connector-mcp-toolbox-postgres",
                "connectors": _supported_connector_ids(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
