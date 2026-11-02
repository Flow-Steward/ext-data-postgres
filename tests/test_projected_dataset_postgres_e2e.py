from __future__ import annotations

import json
import os
import platform
import subprocess  # nosec B404
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# The final extension_host_v2 execution scope: all four keys, at most one owner.
ROOT_STEP_SCOPE = {
    "group_id": None,
    "for_each_id": None,
    "step_id": "read_orders",
    "step_execution_id": "4d3c2b1a-0f9e-4d8c-b7a6-5f4e3d2c0001",
}
FOR_EACH_STEP_SCOPE = {
    **ROOT_STEP_SCOPE,
    "for_each_id": "each_store",
    "step_execution_id": "4d3c2b1a-0f9e-4d8c-b7a6-5f4e3d2c0002",
}

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = BUNDLE_ROOT / "main.py"

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="packaged toolbox binaries are Linux-only; run inside the test container",
)


@pytest.fixture
def toolbox_postgres_connection():
    admin_dsn = (
        os.getenv("FS_TOOLBOX_INTEGRATION_DSN", "").strip() or os.getenv("FS_DB_DSN", "").strip()
    ).replace("postgresql+psycopg://", "postgresql://", 1)
    if not admin_dsn:
        # See the sibling integration module: an absent DSN is "nothing to integrate
        # against", not a failure, or the isolated gate can never be green.
        pytest.skip(
            "set FS_TOOLBOX_INTEGRATION_DSN to run the PostgreSQL end-to-end; through the launcher pass it with --test-env FS_TOOLBOX_INTEGRATION_DSN"
        )
    conninfo = conninfo_to_dict(admin_dsn)
    database_name = f"example_projection_{uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = make_conninfo(admin_dsn, dbname=database_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE orders ("
                "order_id integer PRIMARY KEY, status text NOT NULL, total_amount numeric NOT NULL)"
            )
            connection.execute(
                "INSERT INTO orders (order_id, status, total_amount) VALUES (1001, 'open', 125.5)"
            )
        yield (
            {
                "prebuilt": "postgres",
                "host": str(conninfo["host"]),
                "port": str(conninfo.get("port") or 5432),
                "database": database_name,
                "user": str(conninfo.get("user") or ""),
            },
            {"values": {"password": str(conninfo.get("password") or "")}},
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )


def test_projected_dataset_payload_reaches_packaged_toolbox_postgres(
    toolbox_postgres_connection,
) -> None:
    connection_config, secret_refs = toolbox_postgres_connection
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": connection_config,
                "secret_refs": secret_refs,
                "dataset": {
                    "dataset_id": "projected:install-example:demo_sales_open_orders",
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [
                            {
                                "resource_id": "orders",
                                "columns": ["order_id", "status", "total_amount"],
                            }
                        ],
                    },
                },
                "query_template": {
                    "query_template_id": "projected:install-example:demo_open_orders_query",
                    "query_definition_jsonb": {
                        "sql": (
                            "SELECT order_id, status, total_amount "
                            "FROM orders WHERE status = 'open'"
                        )
                    },
                },
                "limits": {"max_runtime_rows": 10},
            },
        },
    }

    completed = subprocess.run(  # nosemgrep: fixed extension runtime path. # nosec B603
        [sys.executable, str(RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        cwd=BUNDLE_ROOT,
        env=dict(os.environ),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True, payload
    assert payload["result"]["rows"] == [
        {"order_id": 1001, "status": "open", "total_amount": 125.5}
    ]
    assert payload["result"]["row_count"] == 1
