#!/usr/bin/env python3
"""Stateless MCP STDIO test double for the packaged Toolbox process."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def _list_tables() -> list[dict]:
    return [
        {
            "object_details": {
                "object_name": "customers",
                "object_type": "table",
                "columns": [
                    {
                        "column_name": "customer_id",
                        "data_type": "INTEGER",
                        "is_primary_key": True,
                        "is_not_nullable": True,
                        "ordinal_position": 0,
                    },
                    {
                        "column_name": "email",
                        "data_type": "TEXT",
                        "is_not_nullable": True,
                        "ordinal_position": 1,
                    },
                ],
                "indexes": [],
            }
        },
        {
            "object_details": {
                "object_name": "orders",
                "object_type": "table",
                "columns": [
                    {
                        "column_name": "order_id",
                        "data_type": "INTEGER",
                        "is_primary_key": True,
                        "is_not_nullable": True,
                        "ordinal_position": 0,
                    },
                    {
                        "column_name": "status",
                        "data_type": "TEXT",
                        "is_not_nullable": True,
                        "ordinal_position": 1,
                    },
                    {
                        "column_name": "total_amount",
                        "data_type": "REAL",
                        "is_not_nullable": True,
                        "ordinal_position": 2,
                    },
                ],
                "indexes": [{"index_name": "orders_status_idx", "columns": ["status"]}],
            }
        },
    ]


def main() -> int:
    mode = next((arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--mode=")), "")
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "oversized":
        print("x" * (1024 * 1024 + 1), flush=True)
        time.sleep(60)
        return 0
    for line in sys.stdin:
        request = json.loads(line)
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            continue
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stateless-toolbox-test-double", "version": "1"},
            }
        elif method == "tools/call":
            if mode == "result-error":
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "error processing request: unable to execute query: "
                                'ERROR: syntax error at or near "FROM" (SQLSTATE 42601); '
                                f"PASSWORD={os.getenv('POSTGRES_PASSWORD', '')}"
                            ),
                        }
                    ],
                    "isError": True,
                }
                print(
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
                    flush=True,
                )
                continue
            if mode == "secret-error":
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32000,
                                "message": f"connection failed: {os.getenv('POSTGRES_PASSWORD', '')}",
                            },
                        }
                    ),
                    flush=True,
                )
                continue
            tool_name = str((request.get("params") or {}).get("name") or "")
            if tool_name == "list_tables":
                content = _list_tables()
            elif tool_name == "execute_sql":
                content = [] if mode == "empty-write" else [{"order_id": 1001, "status": "open"}]
            else:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32601, "message": f"unknown tool: {tool_name}"},
                        }
                    ),
                    flush=True,
                )
                continue
            if mode == "empty-content":
                result = {"content": []}
            elif mode == "empty-text":
                result = {"content": [{"type": "text", "text": "[]"}]}
            elif mode == "empty-content-success":
                result = {"content": [], "isError": False}
            elif mode == "empty-content-error":
                result = {"content": [], "isError": True}
            elif mode == "empty-json-list":
                result = {"content": [{"type": "text", "text": "[]"}], "isError": False}
            elif mode == "empty-structured":
                result = {"structuredContent": [], "content": [], "isError": False}
            elif mode == "singleton-content-column":
                result = {"structuredContent": {"content": []}, "isError": False}
            elif mode in {"qa-orders", "qa-orders-text"}:
                rows = [
                    {"id": 1, "customer": "Ada", "amount": 12.5, "status": "pending"},
                    {"id": 3, "customer": "Linus", "amount": 7.25, "status": "pending"},
                ]
                result = (
                    {"content": [{"type": "text", "text": json.dumps(rows)}], "isError": False}
                    if mode == "qa-orders-text"
                    else {"structuredContent": rows, "isError": False}
                )
            elif mode == "multiple-text-content":
                result = {
                    "content": [{"type": "text", "text": json.dumps(item)} for item in content],
                    "isError": False,
                }
            else:
                result = {"structuredContent": content, "isError": False}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
