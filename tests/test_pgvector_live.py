"""Opt-in tests only against the dedicated disposable database fixture."""

import os
import json
from pathlib import Path
import uuid
import pytest
import main
import vector_toolbox as toolbox
from vector_commands import tool
from vector_validation import connection_values, Failure

FIXTURE = Path(__file__).with_name("live-pgvector.local.json")
HOST = os.getenv("PGVECTOR_LIVE_HOST") or (
    json.loads(FIXTURE.read_text())["host"] if FIXTURE.exists() else None
)

pytestmark = pytest.mark.skipif(not HOST, reason="Disposable pgvector fixture absent")


def envelope(name, args, *, test_mode=False, claim=True, reference="fixture"):
    context = {
        "account_id": "a",
        "project_id": "p",
        "group_id": None,
        "for_each_id": None,
        "step_id": "s",
        "step_execution_id": uuid.uuid4().hex,
        "test_mode": test_mode,
    }
    if claim:
        context["external_effect_id"] = uuid.uuid4().hex
    return {
        "contract_version": "extension_host_v2",
        "mode": "action",
        "runtime_context": context,
        "action": {
            "action_id": name,
            "input": {"connection_ref": reference, **args},
            "target": {
                "connection": {
                    "connection_id": "fixture",
                    "connection_type": "toolbox_sql_writer",
                    "config": {
                        "prebuilt": "postgres",
                        "host": HOST or "localhost",
                        "user": "vector_test",
                        "port": 5432,
                        "database": "vectors",
                        "schema": "fs_vectors",
                        "tls": False,
                        "request_timeout_seconds": 15,
                    },
                    "secrets": {"password": "fixture-only-pgvector"},
                }
            },
        },
    }


def test_dense_vector_live_lifecycle_and_safety(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    collection = "test_" + uuid.uuid4().hex[:16]

    def invoke(name, **args):
        return main.handle_request(envelope(name, {"collection": collection, **args}))

    def run(name, **args):
        response = invoke(name, **args)
        assert response["ok"], response
        return response["result"]

    try:
        assert run("ensure_collection", dimensions=3)["records"][0]["metric"] == "cosine"
        assert run("ensure_collection", dimensions=3)["applied"]
        assert (
            invoke("ensure_collection", dimensions=2)["error_code"]
            == "index_configuration_mismatch"
        )
        assert (
            invoke("ensure_collection", dimensions=3, metric="l2")["error_code"]
            == "index_configuration_mismatch"
        )
        assert (
            run(
                "upsert_document",
                document_id="doc'--",
                source={"url": "https://example.test/source"},
                chunks=[
                    {
                        "chunk_id": "a",
                        "vector": [1, 0, 0],
                        "content": "literal';drop table x--",
                        "metadata": {"tenant": "one", "nested": {"k": 1}},
                    },
                    {"chunk_id": "b", "vector": [0, 1, 0], "metadata": {"tenant": "two"}},
                ],
            )["count"]
            == 2
        )
        item = run("get_chunk", chunk_id="a", include_vector=True)["records"][0]
        assert item["vector"] == [1, 0, 0] and item["source"]["url"].startswith("https://")
        assert "drop table" in item["content"]
        matches = run(
            "query",
            vector=[1, 0, 0],
            metadata_filter={"tenant": "one", "nested": {"k": 1}},
            min_score=0.9,
            include_vector=True,
        )["records"]
        assert (
            len(matches) == 1
            and matches[0]["chunk_id"] == "a"
            and matches[0]["score"] == pytest.approx(1)
        )
        assert run("query", vector=[1, 0, 0], metadata_filter={"tenant": "missing"})["count"] == 0
        assert run("query", vector=[1, 0, 0], limit=1)["count"] == 1
        assert "vector" not in run("get_chunk", chunk_id="a")["records"][0]
        assert (
            invoke("upsert_chunk", chunk_id="a", document_id="d", vector=[1, 2])["error_code"]
            == "invalid_vector"
        )
        run(
            "update_chunk",
            chunk_id="a",
            vector=[0, 0, 1],
            content="updated",
            metadata={"tenant": "new"},
        )
        assert run("get_chunk", chunk_id="a")["records"][0]["metadata"] == {"tenant": "new"}
        assert (
            run(
                "upsert_chunk",
                chunk_id="b",
                document_id="doc'--",
                vector=[0, 1, 0],
                content="replacement",
            )["count"]
            == 1
        )
        run("delete_chunk", chunk_id="b")
        assert run("get_chunk", chunk_id="b")["count"] == 0
        assert run("delete_document", document_id="doc'--")["records"][0]["deleted_count"] == 1
        assert run("delete_document", document_id="doc'--")["records"][0]["deleted_count"] == 0
        assert run("query", vector=[1, 0, 0])["count"] == 0
        assert run("get_collection")["records"][0]["dimensions"] == 3
    finally:
        run("drop_collection", confirm=True)
    assert invoke("get_collection")["error_code"] == "index_configuration_mismatch"


@pytest.mark.parametrize("metric,expected", [("l2", 0.5), ("inner_product", 2.0)])
def test_metric_scores(metric, expected, monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    collection = "metric_" + uuid.uuid4().hex[:12]

    def run(name, **args):
        result = main.handle_request(envelope(name, {"collection": collection, **args}))
        assert result["ok"], result
        return result["result"]

    try:
        run("ensure_collection", dimensions=2, metric=metric)
        run("upsert_chunk", chunk_id="c", document_id="d", vector=[2, 0])
        result = run("query", vector=[1, 0])
        assert result["records"][0]["score"] == pytest.approx(expected)
    finally:
        run("drop_collection", confirm=True)


def test_native_json_row_wire_budgets(monkeypatch):
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    connection = envelope("get_collection", {"collection": "x"})["action"]["target"]["connection"]
    config, secrets = connection_values(connection)
    for sql in (
        "SELECT repeat('x',500000) AS content",
        "SELECT generate_series(1,101) AS row",
        "SELECT repeat('x',3000000) AS content",
    ):
        with pytest.raises(Failure):
            toolbox.invoke(config, secrets, *tool(sql), timeout_seconds=5)


def test_ordinary_sql_native_retains_more_than_vector_row_limit():
    """The same executable must not apply vector row bounds to SQL datasets."""
    connection = envelope("get_collection", {"collection": "x"})["action"]["target"]["connection"]
    result = main._invoke_toolbox(
        connection["config"],
        {"values": connection["secrets"]},
        "execute_sql",
        {
            "sql": "SELECT generate_series(1,101) AS n",
            "params_json": "{}",
            "param_order": [],
            "binding": "numbered",
        },
        timeout_seconds=5,
    )
    rows = main._toolbox_rows(result)
    assert len(rows) == 101
    assert rows[0] == {"n": 1} and rows[-1] == {"n": 101}


def test_sql_writes_return_rows_and_bigint_repeated_bindings_stay_exact():
    """The combined executable retains public SQL DML and typed parameter contracts."""
    table = "sql_compat_" + uuid.uuid4().hex[:12]
    payload = envelope("execute_write", {})
    connection = payload["action"]["target"]["connection"]
    config, secret_refs = connection["config"], {"values": connection["secrets"]}

    def native(statement):
        return main._invoke_toolbox(
            config,
            secret_refs,
            "execute_sql",
            {"sql": statement, "params_json": "{}", "param_order": [], "binding": "numbered"},
            timeout_seconds=5,
        )

    native(f"CREATE TABLE {table} (id bigint PRIMARY KEY, content text NOT NULL)")
    try:
        value = "literal'$\\; DROP TABLE anything;--"
        payload["action"]["input"] = {
            "connection_ref": "fixture",
            "sql": f"INSERT INTO {table} (id,content) VALUES (:id,:content) RETURNING id,content",
            "params": {"id": 9007199254740993, "content": value},
            "param_schema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "content": {"type": "string"}},
            },
        }
        inserted = main.handle_request(payload)
        assert inserted["ok"], inserted
        assert inserted["result"]["rows"] == [{"id": 9007199254740993, "content": value}]
        assert inserted["external_effect_status"] == "succeeded"
        payload["action"]["action_id"] = "execute_template"
        payload["action"]["input"] = {
            "connection_config": config,
            "secret_refs": secret_refs,
            "dataset": {
                "resource_scope_jsonb": {
                    "resources": [{"resource_id": table, "columns": ["id", "content"]}]
                }
            },
            "query_template": {
                "query_definition_jsonb": {
                    "sql": f"SELECT id, content FROM {table} WHERE id=:id OR id=:id"
                },
                "param_schema_jsonb": {"type": "object", "properties": {"id": {"type": "integer"}}},
            },
            "params": {"id": 9007199254740993},
            "limits": {"max_runtime_rows": 101},
        }
        selected = main.handle_request(payload)
        assert selected["ok"], selected
        assert selected["result"]["rows"] == [{"id": 9007199254740993, "content": value}]
    finally:
        native(f"DROP TABLE {table}")
