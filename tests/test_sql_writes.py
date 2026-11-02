"""Workflow writes use host connections and keep values out of SQL text."""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from test_toolbox_dataset_connector import (
    EXTENSION_ROOT,
    OFFERED_PREBUILTS,
    ROOT_STEP_SCOPE,
    _runtime_module,
)


def request(prebuilt: str = "postgres", **inputs) -> dict:
    return {
        "contract_version": "extension_host_v2",
        "runtime_context": {**ROOT_STEP_SCOPE, "test_mode": False},
        "mode": "action",
        "action": {
            "action_id": "execute_write",
            "target": {
                "connection": {
                    "connection_id": "database-1",
                    "connection_type": "toolbox_sql_writer",
                    "config": {
                        "prebuilt": prebuilt,
                        "host": "database.test",
                        "database": "shop",
                        "user": "writer",
                    },
                    "secrets": {"password": "test-only-password"},
                }
            },
            "input": {
                "connection_ref": "database-1",
                "sql": "UPDATE products SET name = :name WHERE sku = :sku",
                "param_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "sku": {"type": "string"}},
                },
                "params": {"name": "O'Reilly; DELETE FROM products", "sku": "A-1"},
                **inputs,
            },
        },
    }


@pytest.mark.parametrize(
    ("prebuilt", "sql", "binding"),
    [
        ("postgres", "UPDATE products SET name = $1 WHERE sku = $2", "numbered"),
        ("mysql", "UPDATE products SET name = ? WHERE sku = ?", "positional"),
        ("mariadb", "UPDATE products SET name = ? WHERE sku = ?", "positional"),
        ("mssql", "UPDATE products SET name = @p1 WHERE sku = @p2", "named-positional"),
    ],
)
def test_workflow_write_passes_values_separately(monkeypatch, prebuilt, sql, binding):
    if prebuilt not in OFFERED_PREBUILTS:
        pytest.skip("not offered by this variant")
    runtime = _runtime_module()
    calls = []

    def invoke(config, secrets, tool, params, **kwargs):
        calls.append((config, secrets, tool, params))
        return []

    monkeypatch.setattr(runtime, "_invoke_toolbox", invoke)
    response = runtime.handle_request(request(prebuilt))
    assert response["ok"] is True, response
    assert response["external_effect_status"] == "succeeded"
    assert response["result"] == {"status": "executed", "rows": [], "returned_row_count": 0}
    assert len(calls) == 1
    config, secrets, tool, params = calls[0]
    assert config["prebuilt"] == prebuilt
    assert secrets == {"values": {"password": "test-only-password"}}
    assert tool == "execute_sql"
    assert params["sql"] == sql
    assert params["binding"] == binding
    assert json.loads(params["params_json"]) == request()["action"]["input"]["params"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO products (sku, name) VALUES (:sku, :name)",
        "INSERT INTO products (sku, name) VALUES (:sku, :name) ON CONFLICT (sku) DO UPDATE SET name = EXCLUDED.name",
        "DELETE FROM products WHERE sku = :sku AND name = :name",
    ],
)
def test_write_accepts_single_dml_statements(monkeypatch, sql):
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    runtime = _runtime_module()
    monkeypatch.setattr(runtime, "_invoke_toolbox", lambda *args, **kwargs: [{"sku": "A-1"}])
    response = runtime.handle_request(request(sql=sql))
    assert response["ok"] is True, response
    assert response["result"]["rows"] == [{"sku": "A-1"}]
    assert response["result"]["returned_row_count"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM products",
        "DROP TABLE products",
        "TRUNCATE TABLE products",
        "UPDATE products SET name = :name; DELETE FROM products WHERE sku = :sku",
        "BEGIN; UPDATE products SET name = :name; COMMIT",
        "EXEC sp_executesql :name",
        "UPDATE products SET name = :name RETURNING *; SELECT :sku",
        "UPDATE products SET name = :name /* unterminated",
        "",
        "x" * 65537,
    ],
)
def test_invalid_sql_never_reaches_toolbox(monkeypatch, sql):
    runtime = _runtime_module()
    monkeypatch.setattr(
        runtime, "_invoke_toolbox", lambda *a, **k: pytest.fail("must not call database")
    )
    response = runtime.handle_request(request(sql=sql))
    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert response["definitely_no_external_effect"] is True


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "mismatch",
        "wrong_type",
        "unsupported",
        "inline_config",
        "inline_secrets",
        "bad_params",
        "bad_test_mode",
        "bad_prebuilt",
    ],
)
def test_invalid_connection_or_inputs_never_reach_toolbox(monkeypatch, change):
    runtime = _runtime_module()
    payload = request()
    action = payload["action"]
    if change == "missing":
        action["target"] = {}
    elif change == "mismatch":
        action["input"]["connection_ref"] = "other-connection"
    elif change == "wrong_type":
        action["target"]["connection"]["connection_type"] = "other_extension"
    elif change == "unsupported":
        action["target"]["connection"]["config"]["prebuilt"] = "snowflake"
    elif change == "inline_config":
        action["input"]["connection_config"] = {"host": "untrusted.test"}
    elif change == "inline_secrets":
        action["input"]["secret_refs"] = {"password": "untrusted"}
    elif change == "bad_params":
        action["input"]["params"]["name"] = {"sql": "fragment"}
    elif change == "bad_prebuilt":
        action["target"]["connection"]["config"]["prebuilt"] = []
    else:
        payload["runtime_context"]["test_mode"] = "false"
    monkeypatch.setattr(
        runtime, "_invoke_toolbox", lambda *a, **k: pytest.fail("must not call database")
    )
    response = runtime.handle_request(payload)
    assert response["ok"] is False
    assert response["definitely_no_external_effect"] is True


def test_test_mode_suppresses_writes(monkeypatch):
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    runtime = _runtime_module()
    payload = request()
    payload["runtime_context"]["test_mode"] = True
    monkeypatch.setattr(
        runtime, "_invoke_toolbox", lambda *a, **k: pytest.fail("must not call database")
    )
    response = runtime.handle_request(payload)
    assert response["ok"] is True, response
    assert response["external_effect_status"] == "suppressed"
    assert response["definitely_no_external_effect"] is True


def test_write_failure_does_not_claim_a_safe_retry(monkeypatch):
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    runtime = _runtime_module()

    def fail(*args, **kwargs):
        raise RuntimeError("connection lost after sending SQL test-only-password")

    monkeypatch.setattr(runtime, "_invoke_toolbox", fail)
    response = runtime.handle_request(request())
    assert response["ok"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["definitely_no_external_effect"] is False
    assert "test-only-password" not in json.dumps(response)


def test_write_does_not_change_dataset_read_contract():
    manifest = yaml.safe_load((EXTENSION_ROOT / "extension.yaml").read_text())
    contract = manifest["runtime"]["extension_contract_v2"]
    assert contract["dataset_connectors"][0]["capabilities"]["write_queries"] is False
    writable = bool(
        OFFERED_PREBUILTS
        & {
            "postgres",
            "mysql",
            "mariadb",
            "mssql",
            "alloydb-omni",
            "alloydb-postgres",
            "cloud-sql-postgres",
            "cloud-sql-mysql",
            "cloud-sql-mssql",
        }
    )
    actions = yaml.safe_load((EXTENSION_ROOT / "ui/actions/actions.yaml").read_text())["actions"]
    writes = [row for row in actions if row["action_id"] == "execute_write"]
    assert bool(writes) is writable
    if writable:
        assert writes[0]["workflow_visible"] is True
        assert writes[0]["test_mode_behavior"] == "suppress"
        operation = next(
            row for row in contract["operation_manifest"] if row["operation_id"] == "execute_write"
        )
        assert operation["connection_type_ids"] == ["toolbox_sql_writer"]
        assert operation["workflow_visible"] is True
        assert any(row["operation_id"] == "execute_write" for row in manifest["external_effects"])


def test_input_request_is_not_mutated(monkeypatch):
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    runtime = _runtime_module()
    payload = request()
    original = copy.deepcopy(payload)
    monkeypatch.setattr(runtime, "_invoke_toolbox", lambda *a, **k: [])
    assert runtime.handle_request(payload)["ok"] is True
    assert payload == original


@pytest.mark.parametrize("mode", ["empty-write", "empty-text", "empty-content"])
def test_empty_toolbox_result_is_not_returned_as_protocol_metadata(mode):
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    import sys

    runtime = _runtime_module()
    runtime._TOOLBOX_COMMAND_OVERRIDE = [
        sys.executable,
        str(EXTENSION_ROOT / "fixtures/fake_toolbox.py"),
        f"--mode={mode}",
    ]
    response = runtime.handle_request(request())
    assert response["ok"] is True, response
    assert response["result"]["rows"] == []
    assert response["result"]["returned_row_count"] == 0


@pytest.mark.parametrize(
    ("prebuilt", "statement", "expected"),
    [
        (
            "mysql",
            "UPDATE products SET stock=2--1 WHERE sku=:sku",
            "UPDATE products SET stock=2--1 WHERE sku=?",
        ),
        (
            "postgres",
            "INSERT INTO notes(note) VALUES ('a/*literal*/b')",
            "INSERT INTO notes(note) VALUES ('a/*literal*/b')",
        ),
        (
            "mssql",
            "UPDATE [product--notes] SET stock=2 WHERE sku=:sku",
            "UPDATE [product--notes] SET stock=2 WHERE sku=@p1",
        ),
    ],
)
def test_write_preserves_dialect_specific_sql(monkeypatch, prebuilt, statement, expected):
    if prebuilt not in OFFERED_PREBUILTS:
        pytest.skip("not offered")
    runtime = _runtime_module()
    sent = []

    def invoke(config, secrets, tool, params, **kwargs):
        sent.append(params)
        return []

    monkeypatch.setattr(runtime, "_invoke_toolbox", invoke)
    params = {"sku": "A-1"} if ":sku" in statement else {}
    response = runtime.handle_request(request(prebuilt, sql=statement, params=params))
    assert response["ok"] is True, response
    assert sent[0]["sql"] == expected
    assert json.loads(sent[0]["params_json"]) == params


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE products SET stock=2 DELETE FROM products",
        "DELETE FROM products SELECT * FROM products",
        "UPDATE products SET stock=2 SET NOCOUNT ON",
        "UPDATE products SET stock=2 /* comment */ DELETE FROM products",
    ],
)
def test_semicolonless_sqlserver_batches_are_rejected(monkeypatch, statement):
    runtime = _runtime_module()
    monkeypatch.setattr(
        runtime, "_invoke_toolbox", lambda *a, **k: pytest.fail("must not call database")
    )
    response = runtime.handle_request(request("mssql", sql=statement, params={}))
    assert response["ok"] is False
    assert response["definitely_no_external_effect"] is True


@pytest.mark.parametrize("marker", ["/*! DELETE FROM products */", "/*M! DELETE FROM products */"])
def test_mysql_executable_comments_are_rejected(monkeypatch, marker):
    runtime = _runtime_module()
    monkeypatch.setattr(
        runtime, "_invoke_toolbox", lambda *a, **k: pytest.fail("must not call database")
    )
    response = runtime.handle_request(
        request("mysql", sql=f"UPDATE products SET name=:name {marker} WHERE sku=:sku")
    )
    assert response["ok"] is False
    assert response["definitely_no_external_effect"] is True
