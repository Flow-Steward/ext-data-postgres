"""Vector actions share PostgreSQL connections without widening public SQL guards."""

from __future__ import annotations

import copy

import pytest
import yaml

from test_sql_writes import request
from test_toolbox_dataset_connector import EXTENSION_ROOT, _runtime_module


def vector_request(action_id="delete_chunk", **inputs):
    payload = request()
    payload["runtime_context"].update(account_id="account-1", project_id="project-1")
    payload["runtime_context"]["external_effect_id"] = "durable-effect-1"
    payload["action"]["action_id"] = action_id
    payload["action"]["input"] = {
        "connection_ref": "database-1",
        "collection": "documents",
        "chunk_id": "chunk-1",
        **inputs,
    }
    return payload


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("no_claim", "effect_claim_required"),
        ("test_mode", "test_mode_suppressed"),
        ("wrong_reference", "invalid_connection"),
        ("managed_profile", "invalid_connection"),
        ("bad_schema", "invalid_connection"),
        ("bad_tls", "invalid_connection"),
        ("unsafe_query_params", "invalid_connection"),
    ],
)
def test_vector_writes_fail_before_provider_io(monkeypatch, change, expected):
    runtime = _runtime_module()
    payload = vector_request()
    config = payload["action"]["target"]["connection"]["config"]
    if change == "no_claim":
        payload["runtime_context"].pop("external_effect_id")
    elif change == "test_mode":
        payload["runtime_context"]["test_mode"] = True
    elif change == "wrong_reference":
        payload["action"]["input"]["connection_ref"] = "other"
    elif change == "managed_profile":
        config["prebuilt"] = "cloud-sql-postgres"
    elif change == "bad_schema":
        config["schema"] = "bad; DROP TABLE documents"
    elif change == "bad_tls":
        config["tls"] = "false"
    elif change == "unsafe_query_params":
        config["query_params"] = "sslmode=disable&host=another.example"
    original = copy.deepcopy(payload)

    def no_provider(*args, **kwargs):
        pytest.fail("invalid/suppressed vectors must not launch a provider")

    monkeypatch.setattr(runtime.subprocess, "Popen", no_provider)
    result = runtime.handle_request(payload)
    assert result.get("error_code") == expected, result
    assert result["definitely_no_external_effect"] is True
    assert payload == original


def test_vector_actions_use_existing_sql_connection_contract():
    manifest = yaml.safe_load((EXTENSION_ROOT / "extension.yaml").read_text())
    contract = manifest["runtime"]["extension_contract_v2"]
    assert [item["connection_type_id"] for item in contract["connection_types"]] == [
        "toolbox_sql_writer"
    ]
    operations = {item["operation_id"]: item for item in contract["operation_manifest"]}
    vector_names = {
        "ensure_collection",
        "get_collection",
        "drop_collection",
        "upsert_chunk",
        "upsert_document",
        "get_chunk",
        "update_chunk",
        "delete_chunk",
        "delete_document",
        "query",
    }
    assert vector_names <= operations.keys()
    for name in vector_names:
        assert operations[name]["connection_type_ids"] == ["toolbox_sql_writer"]
    assert operations["execute_write"]["connection_type_ids"] == ["toolbox_sql_writer"]
    assert contract["dataset_connectors"][0]["capabilities"]["write_queries"] is False
    assert not contract.get("trigger_types")


def test_vector_uses_writer_user_secret_env_and_closed_transport(monkeypatch):
    import vector_toolbox

    runtime = _runtime_module()
    payload = vector_request(
        "upsert_chunk",
        document_id="document-1",
        vector=[1, 0, 0],
        content="literal'; DROP TABLE documents;--",
    )
    connection = payload["action"]["target"]["connection"]
    connection["config"].update(schema="embeddings", tls=False, request_timeout_seconds=9)
    connection["secrets"] = {"env": {"POSTGRES_PASSWORD": "$secret-password"}}
    original = copy.deepcopy(payload)
    calls = []

    def invoke(config, secrets, command, arguments, **kwargs):
        calls.append((config, secrets, command, arguments, kwargs))
        if len(calls) == 1:
            return [[{"dimensions": 3, "metric": "cosine", "valid": True}]]
        return [[{"chunk_id": "chunk-1", "document_id": "document-1"}]]

    monkeypatch.setattr(vector_toolbox, "invoke", invoke)
    result = runtime.handle_request(payload)
    assert result["ok"], result
    assert result["result"] == {
        "records": [{"chunk_id": "chunk-1", "document_id": "document-1"}],
        "count": 1,
        "applied": True,
    }
    config, secrets, command, arguments, limits = calls[1]
    assert config == {
        "host": "database.test",
        "port": 5432,
        "database": "shop",
        "schema": "embeddings",
        "tls": False,
        "request_timeout_seconds": 9,
    }
    assert secrets == {"username": "writer", "password": "$secret-password"}
    assert '"embeddings"."documents"' in command["statement"]
    assert "DROP TABLE" not in command["statement"]
    assert "DROP TABLE" in arguments["p1"]
    assert command["type"] == "postgres-sql" and command["readOnly"] is False
    assert 0 < limits["timeout_seconds"] <= 9
    assert payload == original


def test_vector_dispatched_write_failure_is_uncertain_without_secrets(monkeypatch):
    import vector_toolbox
    from vector_validation import Failure

    calls = 0

    def invoke(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [[{"dimensions": 3, "metric": "cosine", "valid": True}]]
        raise Failure("provider_unavailable", "Safe provider failure", dispatched=True)

    monkeypatch.setattr(vector_toolbox, "invoke", invoke)
    result = _runtime_module().handle_request(vector_request())
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert result["retryable"] is False
    assert "test-only-password" not in str(result)


def test_vector_calls_share_one_operation_deadline(monkeypatch):
    import vector_runtime
    import vector_toolbox

    now = [0.0]
    remaining = []

    def invoke(*args, timeout_seconds, **kwargs):
        remaining.append(timeout_seconds)
        now[0] += 4.0
        return (
            [[{"dimensions": 3, "metric": "cosine", "valid": True}]] if len(remaining) == 1 else []
        )

    monkeypatch.setattr(vector_runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vector_toolbox, "invoke", invoke)
    result = _runtime_module().handle_request(vector_request())
    assert result["ok"], result
    assert remaining == [15.0, 11.0]


def test_native_provenance_includes_vector_safety_patch():
    import hashlib
    import json

    provenance = json.loads((EXTENSION_ROOT / "toolbox-provenance.json").read_text())
    patch = "scripts/patches/0002-flowsteward-managed-vector-safety.patch"
    assert provenance.get("managed_vector_mode") is True
    assert (
        provenance["patches"][patch]
        == hashlib.sha256((EXTENSION_ROOT / patch).read_bytes()).hexdigest()
    )
    assert (
        "github.com/googleapis/mcp-toolbox/internal/tools/postgres/postgressql"
        in provenance["registered_packages"]
    )


@pytest.mark.parametrize("failure_phase", ["missing_claim", "metadata_lookup"])
def test_pre_dispatch_failure_uses_host_supported_failed_status(monkeypatch, failure_phase):
    """Core confirmation must retain a definite failure rather than metadata conflict."""
    import vector_toolbox
    from vector_validation import Failure

    payload = vector_request()
    if failure_phase == "missing_claim":
        payload["runtime_context"].pop("external_effect_id")
    else:

        def fail_metadata(*args, **kwargs):
            raise Failure("network_blocked", "Endpoint blocked before write dispatch.")

        monkeypatch.setattr(vector_toolbox, "invoke", fail_metadata)
    response = _runtime_module().handle_request(payload)
    assert response["ok"] is False
    assert response["definitely_no_external_effect"] is True
    # This is the host confirmation contract, not an adapter-private phase label.
    assert response["external_effect_status"] == "failed"
    assert response["retryable"] is False


def test_local_vector_fixture_is_excluded_from_release_payload(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pg_package_fixture_exclusion", EXTENSION_ROOT / "scripts/package_extension.py"
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    fixture = tmp_path / "live-pgvector.local.json"
    fixture.write_text('{"host":"local-only","password":"fixture-secret"}')
    (tmp_path / "main.py").write_text("print('runtime')")
    payload = package._payload_paths(tmp_path, "linux-arm64")
    assert fixture not in payload
    assert tmp_path / "main.py" in payload
