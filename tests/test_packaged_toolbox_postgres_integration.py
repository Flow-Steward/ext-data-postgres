from __future__ import annotations

import importlib.util
import os
import platform
from pathlib import Path
from types import ModuleType
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

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="packaged toolbox binaries are Linux-only; run inside the test container",
)


def _runtime_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("toolbox_packaged_integration_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Toolbox runtime module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _integration_connection() -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    raw_dsn = (
        os.getenv("FS_TOOLBOX_INTEGRATION_DSN", "").strip() or os.getenv("FS_DB_DSN", "").strip()
    )
    if not raw_dsn:
        # No database to integrate against. Failing here made this extension's own
        # standard isolated gate permanently RED, which is the one place it is meant
        # to be runnable without external services. A misconfigured DSN still fails.
        pytest.skip(
            "set FS_TOOLBOX_INTEGRATION_DSN to run the PostgreSQL integration; through the launcher pass it with --test-env FS_TOOLBOX_INTEGRATION_DSN"
        )
    raw_dsn = raw_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    conninfo = conninfo_to_dict(raw_dsn)
    if not conninfo.get("host") or not conninfo.get("dbname"):
        pytest.fail("PostgreSQL integration conninfo must include host and dbname")
    config = {
        "prebuilt": "postgres",
        "host": str(conninfo["host"]),
        "port": str(conninfo.get("port") or 5432),
        "database": str(conninfo["dbname"]),
        "user": str(conninfo.get("user") or ""),
    }
    secrets = {"values": {"password": str(conninfo.get("password") or "")}}
    return raw_dsn, config, secrets


def test_packaged_toolbox_introspects_and_queries_disposable_postgres() -> None:
    main = _runtime_module()
    raw_dsn, config, secret_refs = _integration_connection()
    database_name = f"toolbox_integration_{uuid4().hex}"
    table_name = "orders"
    with psycopg.connect(raw_dsn, autocommit=True) as admin_connection:
        admin_connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    disposable_dsn = make_conninfo(raw_dsn, dbname=database_name)
    config["database"] = database_name
    with psycopg.connect(disposable_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE TABLE {} (order_id bigint PRIMARY KEY, status text NOT NULL)").format(
                sql.Identifier(table_name)
            )
        )
        connection.execute(
            sql.SQL(
                "INSERT INTO {} (order_id, status) VALUES (1001, 'open'), (9007199254740993, 'big')"
            ).format(sql.Identifier(table_name))
        )
    common_input = {"connection_config": config, "secret_refs": secret_refs}
    try:
        introspection = main.handle_action(
            {
                "contract_version": "extension_host_v2",
                "runtime_context": dict(FOR_EACH_STEP_SCOPE),
                "mode": "action",
                "action": {"action_id": "introspect_schema", "input": common_input},
            }
        )

        assert introspection["ok"] is True
        resources = introspection["result"]["resources"]
        assert table_name in {row["resource_id"] for row in resources}

        execution = main.handle_action(
            {
                "contract_version": "extension_host_v2",
                "runtime_context": dict(FOR_EACH_STEP_SCOPE),
                "mode": "action",
                "action": {
                    "action_id": "execute_template",
                    "input": {
                        **common_input,
                        "dataset": {
                            "resource_scope_jsonb": {
                                "version": 1,
                                "resources": [{"resource_id": table_name, "columns": None}],
                            }
                        },
                        "query_template": {
                            "query_definition_jsonb": {
                                "sql": (
                                    f"SELECT order_id, status FROM {table_name} "
                                    "WHERE status = :status"
                                )
                            },
                            "param_schema_jsonb": {
                                "type": "object",
                                "properties": {"status": {"type": "string"}},
                                "required": ["status"],
                            },
                        },
                        "params": {"status": "open"},
                        "limits": {"max_runtime_rows": 5},
                    },
                },
            }
        )

        assert execution["ok"] is True
        assert execution["result"]["rows"] == [{"order_id": 1001, "status": "open"}]

        declared_schema = {
            "type": "object",
            "properties": {"order_id": {"type": "integer"}, "status": {"type": "string"}},
        }
        empty_execution = main.handle_action(
            {
                "contract_version": "extension_host_v2",
                "runtime_context": dict(FOR_EACH_STEP_SCOPE),
                "mode": "action",
                "action": {
                    "action_id": "execute_template",
                    "input": {
                        **common_input,
                        "dataset": {
                            "resource_scope_jsonb": {
                                "version": 1,
                                "resources": [{"resource_id": table_name, "columns": None}],
                            }
                        },
                        "query_template": {
                            "query_definition_jsonb": {
                                "sql": (
                                    f"SELECT order_id, status FROM {table_name} "
                                    "WHERE status = :status"
                                )
                            },
                            "param_schema_jsonb": {
                                "type": "object",
                                "properties": {"status": {"type": "string"}},
                                "required": ["status"],
                            },
                            "output_schema_jsonb": declared_schema,
                        },
                        "params": {"status": "missing-status"},
                        "limits": {"max_runtime_rows": 5},
                    },
                },
            }
        )

        assert empty_execution["ok"] is True
        assert empty_execution["result"] == {
            "rows": [],
            "row_count": 0,
            "output_schema": declared_schema,
            "truncated": False,
        }

        big_integer_execution = main.handle_action(
            {
                "contract_version": "extension_host_v2",
                "runtime_context": dict(FOR_EACH_STEP_SCOPE),
                "mode": "action",
                "action": {
                    "action_id": "execute_template",
                    "input": {
                        **common_input,
                        "dataset": {
                            "resource_scope_jsonb": {
                                "version": 1,
                                "resources": [{"resource_id": table_name, "columns": None}],
                            }
                        },
                        "query_template": {
                            "query_definition_jsonb": {
                                "sql": f"SELECT order_id FROM {table_name} WHERE order_id = :order_id"
                            },
                            "param_schema_jsonb": {
                                "type": "object",
                                "properties": {"order_id": {"type": "integer"}},
                                "required": ["order_id"],
                            },
                        },
                        "params": {"order_id": 9007199254740993},
                        "limits": {"max_runtime_rows": 5},
                    },
                },
            }
        )

        assert big_integer_execution["ok"] is True
        assert big_integer_execution["result"]["rows"] == [{"order_id": 9007199254740993}]
    finally:
        with psycopg.connect(raw_dsn, autocommit=True) as admin_connection:
            admin_connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin_connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
            )
