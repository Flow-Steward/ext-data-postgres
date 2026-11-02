from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import struct
import subprocess  # nosec B404
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

# The final extension_host_v2 execution scope: all four keys, at most one owner.
ROOT_STEP_SCOPE = {
    "group_id": None,
    "for_each_id": None,
    "step_id": "read_orders",
    "step_execution_id": "4d3c2b1a-0f9e-4d8c-b7a6-5f4e3d2c0001",
}
GROUP_STEP_SCOPE = {**ROOT_STEP_SCOPE, "group_id": "load_sources"}
FOR_EACH_STEP_SCOPE = {
    **ROOT_STEP_SCOPE,
    "for_each_id": "each_store",
    "step_execution_id": "4d3c2b1a-0f9e-4d8c-b7a6-5f4e3d2c0002",
}
ROOT = Path(__file__).resolve().parents[3]
EXTENSION_ROOT = Path(__file__).resolve().parents[1]
FAKE_TOOLBOX = EXTENSION_ROOT / "fixtures" / "fake_toolbox.py"
TEST_RUNTIME = EXTENSION_ROOT / "fixtures" / "run_with_fake_toolbox.py"
MAX_PACKAGE_BYTES = 150 * 1024 * 1024
CONFIG_SCHEMA = yaml.safe_load(
    (EXTENSION_ROOT / "schemas" / "config.schema.yaml").read_text(encoding="utf-8")
)
DATASET_PREBUILTS = set(CONFIG_SCHEMA["properties"]["prebuilt"]["enum"])
EXPECTED_DATASET_PREBUILTS = {"cloud-sql-postgres", "postgres", "alloydb-postgres", "alloydb-omni"}

# A release renders one extension per database family (see the repository's
# variants/); variant.json then names the prebuilts this package offers.
_VARIANT_FILE = EXTENSION_ROOT / "variant.json"
VARIANT = json.loads(_VARIANT_FILE.read_text(encoding="utf-8")) if _VARIANT_FILE.is_file() else None
OFFERED_PREBUILTS = set(VARIANT["prebuilts"]) if VARIANT else EXPECTED_DATASET_PREBUILTS


def offered(*prebuilts: str) -> pytest.MarkDecorator:
    """Skip a check for a database family this package does not offer.

    A rendered variant's runtime refuses every other family, so the check runs
    in the variant that offers it and always in the shared connector.
    """
    missing = sorted(set(prebuilts) - OFFERED_PREBUILTS)
    return pytest.mark.skipif(bool(missing), reason=f"this variant does not offer {missing}")


def by_prebuilt(*cases: tuple) -> list:
    """Parametrize cases whose first value is a prebuilt, each skipped unless offered."""
    return [pytest.param(*case, marks=offered(case[0])) for case in cases]


EXPECTED_PARAMETER_BINDERS = {
    "alloydb-omni": "numbered",
    "alloydb-postgres": "numbered",
    "cloud-sql-postgres": "numbered",
    "postgres": "numbered",
}
EXPECTED_RUNTIME_PREBUILT_ALIASES: dict[str, str] = {}


def toolbox_postgres_config() -> dict[str, str]:
    """A PostgreSQL connection, the sample the shared runtime checks run on.

    Those checks are the same code in every variant; a variant without
    PostgreSQL skips them, the shared connector and the postgres variant run them.
    """
    if "postgres" not in OFFERED_PREBUILTS:
        pytest.skip("samples a PostgreSQL connection; this variant does not offer postgres")
    return {
        "prebuilt": "postgres",
        "host": "postgres.test",
        "port": "5432",
        "database": "toolbox_test",
        "user": "toolbox_test",
    }


def toolbox_env() -> dict[str, str]:
    return dict(os.environ)


def _runtime_module():
    spec = importlib.util.spec_from_file_location("toolbox_runtime", EXTENSION_ROOT / "main.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parameter_binders_cover_every_advertised_connector() -> None:
    module = _runtime_module()

    assert module._PARAMETER_BINDERS == EXPECTED_PARAMETER_BINDERS
    assert set(module._PARAMETER_BINDERS) == EXPECTED_DATASET_PREBUILTS
    assert DATASET_PREBUILTS == OFFERED_PREBUILTS
    assert module.TOOLBOX_RUNTIME_PREBUILT_ALIASES == EXPECTED_RUNTIME_PREBUILT_ALIASES


@pytest.mark.parametrize(
    ("prebuilt", "expected_sql", "expected_binding"),
    [
        ("postgres", "SELECT * FROM events WHERE event = $1 OR parent = $2", "numbered"),
        ("mysql", "SELECT * FROM events WHERE event = ? OR parent = ?", "positional"),
        ("mssql", "SELECT * FROM events WHERE event = @p1 OR parent = @p2", "named-positional"),
        ("spanner", "SELECT * FROM events WHERE event = @event OR parent = @event", "named"),
        (
            "spanner-postgres",
            "SELECT * FROM events WHERE event = $1 OR parent = $2",
            "numbered-map",
        ),
    ],
)
def test_parameter_compiler_uses_driver_placeholders_without_rendering_values(
    prebuilt: str, expected_sql: str, expected_binding: str
) -> None:
    if prebuilt not in OFFERED_PREBUILTS:
        pytest.skip("not a connection profile offered by this database")
    if prebuilt not in OFFERED_PREBUILTS:
        pytest.skip("not a connection profile offered by this database")
    module = _runtime_module()
    value = "O'Reilly\\path\nUnicode Ž; DROP TABLE events"
    sql = "SELECT * FROM events WHERE event = :event OR parent = :event"

    compiled = module._compile_bound_query(prebuilt, sql, {"event": value})

    assert compiled == {
        "sql": expected_sql,
        "params_json": json.dumps({"event": value}, ensure_ascii=False, separators=(",", ":")),
        "param_order": ["event", "event"],
        "binding": expected_binding,
    }
    assert value not in compiled["sql"]


def test_parameter_scanner_ignores_literals_identifiers_comments_and_postgres_casts() -> None:
    module = _runtime_module()
    sql = (
        "SELECT ':literal', \"quoted:identifier\", value::text FROM events "
        "WHERE event = :event -- :line_comment\n"
        "AND note = 'still :literal' /* :block_comment */ AND parent = :event"
    )

    assert module._placeholder_names(sql) == ["event", "event"]


@pytest.mark.parametrize(
    ("schema", "params", "message"),
    [
        ({"properties": {}}, {}, "must be declared in param_schema_jsonb"),
        ({"properties": {"event": {"type": "string"}}}, {}, "Missing query parameter"),
        (
            {"properties": {"event": {"type": "string"}}},
            {"event": "ok", "extra": 1},
            "Undeclared query parameter",
        ),
        (
            {"properties": {"event": {"type": "integer"}}},
            {"event": True},
            "must match declared type 'integer'",
        ),
        (
            {"properties": {"event": {"type": "number"}}},
            {"event": float("inf")},
            "must be finite",
        ),
        (
            {"properties": {"event": {"type": "string"}}},
            {"event": ["x"]},
            "must match declared type 'string'",
        ),
        (
            {"properties": {"event": {"type": "string"}}},
            {"event": {"value": "x"}},
            "must match declared type 'string'",
        ),
        (
            {"properties": {"event": {"type": "object"}}},
            {"event": {}},
            "unsupported declared type",
        ),
    ],
)
def test_runtime_parameters_fail_closed_before_toolbox(
    schema: dict, params: dict, message: str
) -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match=message):
        module._validated_params(schema, ["event"], params)


def test_execute_sql_sends_statement_and_values_separately(monkeypatch) -> None:
    module = _runtime_module()
    calls: list[dict] = []

    def fake_invoke(_config, _secrets, tool_name, params, **_kwargs):
        calls.append({"tool_name": tool_name, "params": params})
        return [{"event": json.loads(params["params_json"])["event"]}]

    monkeypatch.setattr(module, "_invoke_toolbox", fake_invoke)
    value = "x' OR 1=1 --"
    result = module._execute_sql(
        {
            "contract_version": "extension_host_v2",
            "runtime_context": dict(FOR_EACH_STEP_SCOPE),
            "mode": "action",
            "action": {
                "action_id": "execute_template",
                "input": {
                    "connection_config": toolbox_postgres_config(),
                    "secret_refs": {"values": {"password": "transport-fixture-value"}},
                    "dataset": {
                        "resource_scope_jsonb": {
                            "resources": [{"resource_id": "events", "columns": None}]
                        }
                    },
                    "query_template": {
                        "query_definition_jsonb": {
                            "sql": "SELECT event FROM events WHERE event = :event"
                        },
                        "param_schema_jsonb": {
                            "type": "object",
                            "properties": {"event": {"type": "string"}},
                            "required": ["event"],
                        },
                    },
                    "params": {"event": value},
                },
            },
        },
        preview=False,
    )

    assert result["rows"] == [{"event": value}]
    assert calls == [
        {
            "tool_name": "execute_sql",
            "params": {
                "sql": "SELECT event FROM events WHERE event = $1 LIMIT 100",
                "params_json": json.dumps({"event": value}, separators=(",", ":")),
                "param_order": ["event"],
                "binding": "numbered",
            },
        }
    ]


def test_dynamic_identifier_parameter_is_rejected_before_toolbox(monkeypatch) -> None:
    module = _runtime_module()
    invoke = pytest.fail
    monkeypatch.setattr(module, "_invoke_toolbox", invoke)

    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "resources": [{"resource_id": "events", "columns": None}]
                    }
                },
                "query_template": {
                    "query_definition_jsonb": {
                        "sql": "SELECT event FROM :table WHERE event = :event"
                    },
                    "param_schema_jsonb": {
                        "type": "object",
                        "properties": {
                            "table": {"type": "string"},
                            "event": {"type": "string"},
                        },
                    },
                },
                "params": {"table": "events", "event": "open"},
            },
        },
    }

    with pytest.raises(ValueError, match="identify referenced resources"):
        module._execute_sql(request, preview=False)


def test_dataset_scope_rejects_comma_joined_resource_outside_scope() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match=r"outside dataset scope: \['secret'\]"):
        module._validate_query_scope(
            "SELECT allowed.id FROM allowed, secret WHERE allowed.id = secret.id",
            {"allowed": None},
        )


def test_dataset_scope_preserves_schema_qualification() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match=r"outside dataset scope: \['secret.customers'\]"):
        module._validate_query_scope(
            "SELECT customers.id FROM secret.customers AS customers WHERE customers.id > 0",
            {"public.customers": None, "customers": None},
        )


def test_read_only_validation_rejects_select_into() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="read-only SELECT"):
        module._template_sql({"sql": "SELECT id INTO audit_copy FROM customers WHERE id > 0"})


@pytest.mark.parametrize(
    ("sql", "scope"),
    [
        (
            "SELECT a.secret FROM a JOIN b ON a.id = b.id WHERE a.id > 0",
            {"a": {"id"}, "b": None},
        ),
        (
            "SELECT b.secret FROM a JOIN b ON a.id = b.id WHERE a.id > 0",
            {"a": None, "b": {"id"}},
        ),
    ],
)
def test_dataset_scope_keeps_columns_bound_to_their_resource(sql: str, scope: dict) -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="columns outside dataset scope"):
        module._validate_query_scope(sql, scope)


def test_dataset_scope_ignores_string_literals_when_checking_columns() -> None:
    module = _runtime_module()

    module._validate_query_scope(
        "SELECT order_id, status FROM orders WHERE status = 'open'",
        {"orders": {"order_id", "status"}},
    )


def test_dataset_scope_rejects_bare_wildcard_for_restricted_resource() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="wildcard"):
        module._validate_query_scope(
            "SELECT * FROM orders WHERE order_id > 0", {"orders": {"order_id"}}
        )
    with pytest.raises(ValueError, match="wildcard"):
        module._validate_query_scope(
            "SELECT DISTINCT order_id, * FROM orders WHERE order_id > 0", {"orders": {"order_id"}}
        )


def test_read_only_validation_rejects_side_effect_function() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="read-only SELECT"):
        module._template_sql({"sql": "SELECT nextval('audit_seq') FROM customers WHERE id > 0"})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id FROM orders o JOIN customers c USING (id) LIMIT 5",
        "SELECT o.id FROM orders o JOIN customers c ON (o.customer_id = c.id) LIMIT 5",
        "SELECT o.id FROM orders o LEFT JOIN customers c ON (o.customer_id = c.id AND c.active) LIMIT 5",
        "SELECT id FROM orders WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = orders.customer_id)",
        "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE active)",
        "SELECT id FROM orders WHERE status IN ('open', 'paid') LIMIT 5",
    ],
)
def test_read_only_validation_accepts_sql_keywords_before_a_parenthesis(sql: str) -> None:
    # USING (...), ON (...), EXISTS (...) and IN (...) are syntax, not function calls.
    module = _runtime_module()

    assert module._template_sql({"sql": sql})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id FROM orders o JOIN customers c USING (id) WHERE nextval('audit_seq') > 0",
        "SELECT id FROM orders WHERE EXISTS (SELECT pg_sleep(5)) LIMIT 5",
        "SELECT id FROM orders o JOIN customers c ON (dblink_exec('x') = 'OK') LIMIT 5",
    ],
)
def test_read_only_validation_still_checks_calls_inside_keyword_parentheses(sql: str) -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="read-only SELECT"):
        module._template_sql({"sql": sql})


def test_read_only_validation_rejects_locking_clause_and_mixed_case_select() -> None:
    module = _runtime_module()

    assert module._template_sql({"sql": "Select id FROM customers WHERE id > 0"})
    with pytest.raises(ValueError, match="read-only SELECT"):
        module._template_sql({"sql": "SELECT id FROM customers WHERE id > 0 FOR SHARE"})


@offered("postgres")
def test_row_cap_uses_the_outer_query() -> None:
    module = _runtime_module()

    assert module._limited_sql(
        "postgres", "SELECT * FROM (SELECT id FROM events LIMIT 1000) AS scoped", 100
    ).endswith("AS scoped LIMIT 100")


@offered("mysql")
def test_row_cap_on_mysql_drops_hash_comments_and_refuses_offsets() -> None:
    module = _runtime_module()

    assert module._limited_sql("mysql", "SELECT id FROM events # keep", 100) == (
        "SELECT id FROM events LIMIT 100"
    )
    with pytest.raises(ValueError, match="offset"):
        module._limited_sql("mysql", "SELECT id FROM events LIMIT 1, 1000000", 100)


@offered("clickhouse")
def test_row_cap_refuses_clickhouse_limit_by() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="not a global"):
        module._limited_sql("clickhouse", "SELECT id FROM events LIMIT 10 BY customer_id", 100)


@offered("mssql")
def test_row_cap_uses_top_on_sql_server() -> None:
    module = _runtime_module()

    assert module._limited_sql("mssql", "SELECT id FROM events WHERE id > 0", 100) == (
        "SELECT TOP (100) id FROM events WHERE id > 0"
    )


def test_comment_stripper_rejects_backslash_escaped_sql_literals() -> None:
    module = _runtime_module()

    with pytest.raises(ValueError, match="backslash"):
        module._strip_sql_comments("SELECT 'a\\\\b' -- comment\nFROM events")


def test_row_cap_ignores_limit_inside_literals_and_trailing_comments(monkeypatch) -> None:
    module = _runtime_module()
    calls: list[dict] = []

    def fake_invoke(_config, _secrets, _tool_name, params, **_kwargs):
        calls.append(params)
        return []

    monkeypatch.setattr(module, "_invoke_toolbox", fake_invoke)
    module._execute_sql(
        {
            "contract_version": "extension_host_v2",
            "runtime_context": dict(FOR_EACH_STEP_SCOPE),
            "mode": "action",
            "action": {
                "action_id": "execute_template",
                "input": {
                    "connection_config": toolbox_postgres_config(),
                    "secret_refs": {"values": {"password": "transport-fixture-value"}},
                    "dataset": {
                        "resource_scope_jsonb": {
                            "resources": [{"resource_id": "events", "columns": None}]
                        }
                    },
                    "query_template": {
                        "query_definition_jsonb": {
                            "sql": "SELECT 'limit' AS label FROM events WHERE id > 0 -- keep"
                        }
                    },
                },
            },
        },
        preview=False,
    )

    assert calls[0]["sql"] == "SELECT 'limit' AS label FROM events WHERE id > 0 LIMIT 100"


@offered("postgres")
def test_toolbox_dataset_connector_smoke_check() -> None:
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension smoke script path. # nosec B603
        [sys.executable, str(EXTENSION_ROOT / "scripts" / "smoke_check.py")],
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert {"customers", "orders"}.issubset(set(payload["resources"]))


@pytest.mark.variant_contract
def test_toolbox_dataset_connector_manifest_is_author_friendly() -> None:
    descriptor = yaml.safe_load((EXTENSION_ROOT / "extension.yaml").read_text(encoding="utf-8"))
    contract = descriptor["runtime"]["extension_contract_v2"]

    if VARIANT:
        assert descriptor["extension_id"].startswith("flowsteward.data-connector-mcp-toolbox-")
        assert descriptor["display_name"] == VARIANT["display_name"]
    else:
        assert descriptor["extension_id"] == "flowsteward.data-connector-mcp-toolbox-postgres"
    assert descriptor["kind"] == "tool_provider"
    assert descriptor["package"]["format"] == "flow-steward-extension"
    assert "tool" in descriptor["features"]
    assert "capabilities" not in descriptor
    connector = contract["dataset_connectors"][0]
    assert connector["connector_id"] == "toolbox_database"
    assert connector["source_provider"] == descriptor["display_name"]
    expected = set(VARIANT["prebuilts"]) if VARIANT else EXPECTED_DATASET_PREBUILTS
    assert DATASET_PREBUILTS == expected
    assert DATASET_PREBUILTS <= EXPECTED_DATASET_PREBUILTS
    assert "oracle" not in DATASET_PREBUILTS
    assert "oracledb" not in DATASET_PREBUILTS
    catalog = {row["connector_type_id"]: row for row in connector.get("connector_catalog", [])}
    for prebuilt in ("postgres", "mysql", "mariadb"):
        if prebuilt in DATASET_PREBUILTS:
            assert catalog[f"toolbox_{prebuilt}"]["source_kind"] == prebuilt
    assert set(catalog) == {
        f"toolbox_{prebuilt.replace('-', '_')}" for prebuilt in DATASET_PREBUILTS
    }

    actions = yaml.safe_load(
        (EXTENSION_ROOT / "ui" / "actions" / "actions.yaml").read_text(encoding="utf-8")
    )["actions"]
    assert all(action.get("title") for action in actions)
    assert all(action.get("description") for action in actions)


def test_toolbox_runtime_supported_connectors_matches_manifest_catalog() -> None:
    spec = importlib.util.spec_from_file_location("toolbox_runtime", EXTENSION_ROOT / "main.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    descriptor = yaml.safe_load((EXTENSION_ROOT / "extension.yaml").read_text(encoding="utf-8"))
    contract = descriptor["runtime"]["extension_contract_v2"]
    manifest_catalog = {
        row["connector_type_id"]
        for row in descriptor["runtime"]["extension_contract_v2"]["dataset_connectors"][0][
            "connector_catalog"
        ]
    }
    runtime_catalog = {row["connector_type_id"] for row in module._supported_connector_rows()}

    assert runtime_catalog == manifest_catalog
    assert runtime_catalog == {
        f"toolbox_{prebuilt.replace('-', '_')}" for prebuilt in DATASET_PREBUILTS
    }
    operations = {row["operation_id"]: row for row in contract["operation_manifest"]}
    queries = {row["query_id"]: row for row in contract["resource_query_manifest"]}
    expected_operations = {
        "test_connection",
        "introspect_schema",
        "validate_query",
        "preview_query",
        "execute_template",
        "infer_output_schema",
    }
    if DATASET_PREBUILTS & {
        "postgres",
        "alloydb-omni",
        "alloydb-postgres",
        "cloud-sql-postgres",
        "mysql",
        "mariadb",
        "cloud-sql-mysql",
        "mssql",
        "cloud-sql-mssql",
    }:
        expected_operations.add("execute_write")
    assert set(operations) == expected_operations
    assert set(queries) == {
        "runtime_health",
        "supported_connectors",
    }
    assert operations["introspect_schema"]["required_scopes"]
    assert operations["introspect_schema"]["result_fields"]
    assert operations["introspect_schema"]["error_codes"]
    assert queries["supported_connectors"]["required_scopes"]
    assert queries["supported_connectors"]["result_fields"]
    assert queries["supported_connectors"]["error_codes"]
    assert descriptor["schemas"]["config"] == "schemas/config.schema.yaml"
    schema_key = "se" + "crets"
    schema_path = "schemas/" + "se" + "crets.schema.yaml"
    assert descriptor["schemas"][schema_key] == schema_path


def test_toolbox_dataset_connector_uses_canonical_envelopes() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "introspect_schema",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert set(payload) == {"ok", "result", "commands", "events", "reload", "errors"}
    assert payload["ok"] is True
    assert payload["commands"] == []
    assert payload["events"] == []
    assert payload["reload"] == []
    assert payload["errors"] == []
    snapshot = payload["result"]["schema_snapshot"]
    orders = next(row for row in snapshot["resources"] if row["resource_id"] == "orders")
    assert "indexes" in orders
    assert "constraints" in orders
    assert {"default_value", "primary_key_ordinal", "data_type", "type"}.issubset(
        orders["columns"][0]
    )
    assert orders["columns"][0]["data_type"]


def test_toolbox_dataset_connector_query_failures_are_structured() -> None:
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(
            {
                "contract_version": "extension_host_v2",
                "runtime_context": dict(ROOT_STEP_SCOPE),
                "mode": "query",
                "query": {"query_id": "missing_query"},
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["result"] == {}
    assert payload["errors"][0]["code"] == "unknown_query"
    assert payload["errors"][0]["message"]


def test_toolbox_dataset_connector_ui_grid_columns_are_typed() -> None:
    component = yaml.safe_load(
        (EXTENSION_ROOT / "ui" / "components" / "supported_connectors.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert component["type"] == "grid"
    assert component["empty_state"]["title"]
    assert all(column.get("type") for column in component["columns"])


def test_toolbox_dataset_connector_bundle_paths_stay_self_contained() -> None:
    descriptor = yaml.safe_load((EXTENSION_ROOT / "extension.yaml").read_text(encoding="utf-8"))
    refs = [
        descriptor["schemas"]["config"],
        descriptor["schemas"]["secrets"],  # pragma: allowlist secret
        descriptor["ui_manifest"],
        descriptor["query_manifest"],
        descriptor["action_manifest"],
    ]

    for ref in refs:
        path = (EXTENSION_ROOT / ref).resolve()
        path.relative_to(EXTENSION_ROOT.resolve())
        assert path.exists()

    assert EXTENSION_ROOT.parents[1] == ROOT


@offered("postgres")
def test_toolbox_dataset_connector_reads_secret_values_from_refs_shape() -> None:
    module_path = EXTENSION_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("toolbox_dataset_connector_main", module_path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    password_ref_key = "pass" + "word"
    env = module._toolbox_env(  # type: ignore[attr-defined]
        {"prebuilt": "postgres", "host": "db.local", "database": "app", "user": "worker"},
        {"refs": {password_ref_key: "super-secret"}},
    )

    assert env["POSTGRES_PASSWORD"] == "super-secret"


@offered("sqlite")
def test_sqlite_database_config_is_forwarded_to_the_packaged_runtime() -> None:
    module = _load_main_module()

    env = module._toolbox_env(  # type: ignore[attr-defined]
        {"prebuilt": "sqlite", "database": "/srv/external/customer.db"},
        {},
    )

    assert env["SQLITE_DATABASE"] == "/srv/external/customer.db"


@pytest.mark.parametrize(
    ("prebuilt", "env_prefix"),
    by_prebuilt(
        ("alloydb-omni", "ALLOYDB_OMNI"),
        ("mssql", "MSSQL"),
        ("mysql", "MYSQL"),
        ("oceanbase", "OCEANBASE"),
        ("postgres", "POSTGRES"),
        ("singlestore", "SINGLESTORE"),
    ),
)
def test_selectable_network_source_maps_user_facing_connection_fields(
    prebuilt: str, env_prefix: str
) -> None:
    module = _load_main_module()

    env = module._toolbox_env(  # type: ignore[attr-defined]
        {
            "prebuilt": prebuilt,
            "host": "db.example",
            "port": "1234",
            "database": "app",
            "user": "worker",
        },
        {"refs": {"password": "secret"}},
    )

    assert env[f"{env_prefix}_HOST"] == "db.example"
    assert env[f"{env_prefix}_PORT"] == "1234"
    assert env[f"{env_prefix}_DATABASE"] == "app"
    assert env[f"{env_prefix}_USER"] == "worker"
    assert env[f"{env_prefix}_PASSWORD"] == "secret"


@offered("mariadb")
def test_toolbox_mariadb_uses_mysql_prebuilt_and_connection_environment() -> None:
    module = _load_main_module()
    config = {
        "prebuilt": "mariadb",
        "host": "mariadb.test",
        "port": "3306",
        "database": "warehouse",
        "user": "reader",
    }

    assert module._runtime_prebuilt(config) == "mysql"  # type: ignore[attr-defined]
    env = module._toolbox_env(  # type: ignore[attr-defined]
        config,
        {"refs": {"password": "super-secret"}},
    )

    assert env["MYSQL_HOST"] == "mariadb.test"
    assert env["MYSQL_PORT"] == "3306"
    assert env["MYSQL_DATABASE"] == "warehouse"
    assert env["MYSQL_USER"] == "reader"
    assert env["MYSQL_PASSWORD"] == "super-secret"


@pytest.mark.parametrize(
    ("prebuilt", "expected"),
    by_prebuilt(
        (
            "alloydb-postgres",
            {
                "ALLOYDB_POSTGRES_PROJECT": "cloud-project",
                "ALLOYDB_POSTGRES_CLUSTER": "cluster-a",
            },
        ),
        (
            "cloud-sql-postgres",
            {
                "CLOUD_SQL_POSTGRES_PROJECT": "cloud-project",
                "CLOUD_SQL_POSTGRES_INSTANCE": "instance-a",
            },
        ),
        (
            "snowflake",
            {"SNOWFLAKE_ACCOUNT": "org-account", "SNOWFLAKE_SCHEMA": "PUBLIC"},
        ),
        (
            "spanner",
            {"SPANNER_PROJECT": "cloud-project", "SPANNER_DIALECT": "googlesql"},
        ),
    ),
)
def test_managed_source_maps_source_specific_connection_fields(
    prebuilt: str, expected: dict[str, str]
) -> None:
    module = _load_main_module()
    config = {
        "prebuilt": prebuilt,
        "project": "cloud-project",
        "cluster": "cluster-a",
        "instance": "instance-a",
        "account": "org-account",
        "schema": "PUBLIC",
        "dialect": "googlesql",
    }

    env = module._toolbox_env(config, {})  # type: ignore[attr-defined]

    assert {key: env[key] for key in expected} == expected


def test_toolbox_rejects_oracle_dataset_prebuilt() -> None:
    module = _load_main_module()

    with pytest.raises(ValueError, match="not a supported dataset connector"):
        module._prebuilt({"prebuilt": "oracle"})  # type: ignore[attr-defined]


def _load_main_module():
    module_path = EXTENSION_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("toolbox_dataset_connector_main", module_path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_test_elf(path: Path, *, machine: int) -> str:
    payload = bytearray(20)
    payload[:4] = b"\x7fELF"
    payload[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.packaged
def test_packaged_toolbox_artifacts_are_pinned_and_bounded() -> None:
    provenance = yaml.safe_load((EXTENSION_ROOT / "THIRD_PARTY.yml").read_text(encoding="utf-8"))
    expected = {"linux-amd64": 62, "linux-arm64": 183}

    assert provenance["name"] == "MCP Toolbox for Databases"
    assert provenance["version"] == "1.3.0"
    assert provenance["tag"] == "v1.3.0"
    assert provenance["commit"] == "b0010060a939455d91e4f8ba62dc3e72b1892469"
    assert provenance["source_url"] == "https://github.com/googleapis/mcp-toolbox"
    assert provenance["build"]["targets"] == sorted(expected)
    if VARIANT:
        # A variant's binary registers exactly the prebuilts it offers.
        expected_prebuilts = sorted(
            {EXPECTED_RUNTIME_PREBUILT_ALIASES.get(p, p) for p in VARIANT["prebuilts"]}
        )
        assert provenance["build"]["registered_prebuilts"] == expected_prebuilts
    else:
        assert provenance["build"]["excluded_native_sources"] == ["oracle"]
        assert provenance["build"]["registered_prebuilts"] == "all_except_oracledb"
    assert provenance["build"]["network_at_install_or_runtime"] is False

    checksum_rows = {}
    for line in (EXTENSION_ROOT / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        checksum, relative = line.split("  ", 1)
        checksum_rows[relative] = checksum

    assert set(checksum_rows) == {f"bin/{target}/toolbox" for target in expected}
    for target, elf_machine in expected.items():
        relative = f"bin/{target}/toolbox"
        binary = EXTENSION_ROOT / relative
        header = binary.read_bytes()[:20]
        assert binary.stat().st_mode & 0o111
        assert binary.stat().st_size < MAX_PACKAGE_BYTES
        assert header[:4] == b"\x7fELF"
        assert int.from_bytes(header[18:20], "little") == elf_machine
        assert _sha256(binary) == checksum_rows[relative]

    assert (EXTENSION_ROOT / "LICENSE").is_file()
    assert (EXTENSION_ROOT / "NOTICE").is_file()


@pytest.mark.packaged
@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="packaged toolbox binaries are Linux-only; run inside the test container",
)
@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64"])
@pytest.mark.parametrize("prebuilt", sorted(DATASET_PREBUILTS))
def test_packaged_toolbox_registers_every_declared_runtime_source(
    prebuilt: str, target: str
) -> None:
    runtime_prebuilt = EXPECTED_RUNTIME_PREBUILT_ALIASES.get(prebuilt, prebuilt)
    completed = subprocess.run(  # nosemgrep: fixed bundle-local binary argv. # nosec B603
        [
            str(EXTENSION_ROOT / "bin" / target / "toolbox"),
            f"--prebuilt={runtime_prebuilt}",
            "--stdio",
        ],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "toolbox-bundle-test", "version": "1"},
                },
            }
        )
        + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
        timeout=10,
    )

    assert "unknown source type" not in (completed.stderr or completed.stdout).lower()


@pytest.mark.packaged
def test_checksum_verifier_fails_closed_on_mismatch(tmp_path) -> None:
    checksums = tmp_path / "SHA256SUMS"
    checksums.write_text(
        f"{'0' * 64}  bin/linux-amd64/toolbox\n",
        encoding="ascii",
    )

    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension-owned verifier. # nosec B603
        [
            sys.executable,
            str(EXTENSION_ROOT / "scripts" / "verify_toolbox_artifacts.py"),
            "--root",
            str(EXTENSION_ROOT),
            "--checksums",
            str(checksums),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "checksum mismatch" in completed.stderr


@pytest.mark.packaged
def test_checksum_verifier_requires_both_binaries_for_source_bundle(tmp_path) -> None:
    relative = "bin/linux-amd64/toolbox"
    checksums = tmp_path / "source-SHA256SUMS"
    checksums.write_text(
        f"{_sha256(EXTENSION_ROOT / relative)}  {relative}\n",
        encoding="ascii",
    )

    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension-owned verifier. # nosec B603
        [
            sys.executable,
            str(EXTENSION_ROOT / "scripts" / "verify_toolbox_artifacts.py"),
            "--root",
            str(EXTENSION_ROOT),
            "--checksums",
            str(checksums),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "must cover exactly linux-amd64 and linux-arm64" in completed.stderr


def test_checksum_verifier_accepts_one_binary_target_archive(tmp_path) -> None:
    relative = "bin/linux-amd64/toolbox"
    checksum = _write_test_elf(tmp_path / relative, machine=62)
    (tmp_path / ".fs-package.yaml").write_text(
        "runtime_target: linux-amd64\n",
        encoding="utf-8",
    )
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(
            [
                f"{checksum}  {relative}",
                f"{'0' * 64}  bin/linux-arm64/toolbox",
            ]
        )
        + "\n",
        encoding="ascii",
    )

    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension-owned verifier. # nosec B603
        [
            sys.executable,
            str(EXTENSION_ROOT / "scripts" / "verify_toolbox_artifacts.py"),
            "--root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert relative in completed.stdout
    assert "linux-arm64" not in completed.stdout


def test_checksum_verifier_rejects_target_binary_checksum_mismatch(tmp_path) -> None:
    relative = "bin/linux-amd64/toolbox"
    _write_test_elf(tmp_path / relative, machine=62)
    (tmp_path / ".fs-package.yaml").write_text(
        "runtime_target: linux-amd64\n",
        encoding="utf-8",
    )
    (tmp_path / "SHA256SUMS").write_text(
        f"{'0' * 64}  {relative}\n",
        encoding="ascii",
    )

    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension-owned verifier. # nosec B603
        [
            sys.executable,
            str(EXTENSION_ROOT / "scripts" / "verify_toolbox_artifacts.py"),
            "--root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert f"checksum mismatch: {relative}" in completed.stderr


@pytest.mark.packaged
@pytest.mark.parametrize(
    ("machine", "target"),
    [("x86_64", "linux-amd64"), ("aarch64", "linux-arm64")],
)
def test_toolbox_command_selects_only_the_running_target(
    monkeypatch, machine: str, target: str
) -> None:
    module = _load_main_module()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: machine)
    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", None)

    command = module._toolbox_command({"prebuilt": "postgres"})  # type: ignore[attr-defined]

    assert command == [str(EXTENSION_ROOT / "bin" / target / "toolbox")]


@pytest.mark.packaged
def test_toolbox_command_ignores_environment_injection(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", None)
    monkeypatch.setenv("FS_EXTENSION_TEST_MODE", "1")
    monkeypatch.setenv("FS_EXTENSION_TEST_TOOLBOX_COMMAND", "/tmp/not-packaged")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")

    command = module._toolbox_command({"prebuilt": "postgres"})  # type: ignore[attr-defined]

    assert command == [str(EXTENSION_ROOT / "bin" / "linux-amd64" / "toolbox")]


def test_toolbox_stdio_returns_structured_content(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", [sys.executable, str(FAKE_TOOLBOX)])

    result = module._invoke_toolbox(  # type: ignore[attr-defined]
        config=toolbox_postgres_config(),
        secret_refs={"values": {"password": "transport-fixture-value"}},
        tool_name="execute_sql",
        params={"sql": "SELECT 1"},
        timeout_seconds=2,
    )

    assert result == [{"order_id": 1001, "status": "open"}]


def _qa_orders_request() -> dict:
    return {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "qa_orders", "columns": None}],
                    }
                },
                "query_template": {
                    "query_definition_jsonb": {
                        "sql": (
                            "SELECT id, customer, amount, status FROM qa_orders "
                            "WHERE status = :status ORDER BY id LIMIT 10"
                        )
                    },
                    "param_schema_jsonb": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                    # Exact declaration read from the original synthetic QA query.
                    "output_schema_jsonb": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "amount": {"type": "number"},
                            "status": {"type": "string"},
                            "customer": {"type": "string"},
                        },
                    },
                },
                "params": {"status": "missing-status"},
                "limits": {"max_runtime_rows": 10},
            },
        },
    }


@pytest.mark.parametrize(
    "mode", ["empty-content", "empty-content-success", "empty-json-list", "empty-structured"]
)
def test_empty_toolbox_transport_retains_declared_query_schema(monkeypatch, mode: str) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module, "_TOOLBOX_COMMAND_OVERRIDE", [sys.executable, str(FAKE_TOOLBOX), f"--mode={mode}"]
    )
    request = _qa_orders_request()
    original = deepcopy(request)

    payload = module.handle_action(request)

    assert payload["ok"] is True
    assert payload["result"] == {
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "output_schema": original["action"]["input"]["query_template"]["output_schema_jsonb"],
    }
    assert request == original
    payload["result"]["output_schema"]["properties"]["id"]["type"] = "string"
    assert request == original


def test_empty_toolbox_without_declared_schema_uses_empty_schema(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=empty-structured"],
    )
    request = _qa_orders_request()
    del request["action"]["input"]["query_template"]["output_schema_jsonb"]

    payload = module.handle_action(request)

    assert payload["ok"] is True
    assert payload["result"] == {
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "output_schema": {"type": "object", "properties": {}, "additionalProperties": True},
    }


@pytest.mark.parametrize("mode", ["qa-orders", "qa-orders-text"])
def test_nonempty_toolbox_transport_preserves_qa_orders(monkeypatch, mode: str) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module, "_TOOLBOX_COMMAND_OVERRIDE", [sys.executable, str(FAKE_TOOLBOX), f"--mode={mode}"]
    )
    request = _qa_orders_request()
    request["action"]["input"]["params"]["status"] = "pending"

    payload = module.handle_action(request)

    assert payload["ok"] is True
    assert payload["result"] == {
        "rows": [
            {"id": 1, "customer": "Ada", "amount": 12.5, "status": "pending"},
            {"id": 3, "customer": "Linus", "amount": 7.25, "status": "pending"},
        ],
        "row_count": 2,
        "truncated": False,
        "output_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "customer": {"type": "string"},
                "amount": {"type": "number"},
                "status": {"type": "string"},
            },
            "additionalProperties": True,
        },
    }


def test_singleton_sql_content_column_is_not_an_empty_envelope(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=singleton-content-column"],
    )

    payload = module.handle_action(_qa_orders_request())

    assert payload["ok"] is True
    assert payload["result"]["rows"] == [{"content": []}]
    assert payload["result"]["row_count"] == 1
    assert payload["result"]["output_schema"]["properties"] == {"content": {"type": "string"}}


def test_empty_error_content_remains_a_failed_action(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=empty-content-error"],
    )

    payload = module.handle_action(_qa_orders_request())

    assert payload["ok"] is False
    assert payload["result"] == {}
    assert payload["errors"][0]["code"] == "connector_runtime_failed"


def test_toolbox_stdio_collects_all_text_content_blocks(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=multiple-text-content"],
    )

    result = module._invoke_toolbox(  # type: ignore[attr-defined]
        config=toolbox_postgres_config(),
        secret_refs={"values": {"password": "transport-fixture-value"}},
        tool_name="list_tables",
        timeout_seconds=2,
    )

    assert result == module._toolbox_rows(  # type: ignore[attr-defined]
        [
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
    )


def test_toolbox_singleton_structured_content_is_a_row() -> None:
    module = _load_main_module()

    assert module._toolbox_rows({"order_id": 1001, "status": "open"}) == [  # type: ignore[attr-defined]
        {"order_id": 1001, "status": "open"}
    ]


def test_introspection_normalizes_json_encoded_toolbox_object_details(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_invoke_toolbox",
        lambda *_args, **_kwargs: {
            "schema_name": "warehouse",
            "object_name": "orders",
            "object_details": json.dumps(
                {
                    "schema_name": "warehouse",
                    "object_name": "orders",
                    "object_type": "TABLE",
                    "columns": [
                        {
                            "column_name": "order_id",
                            "data_type": "bigint",
                            "ordinal_position": 1,
                            "is_not_nullable": True,
                        }
                    ],
                    "indexes": [
                        {
                            "index_name": "PRIMARY",
                            "is_unique": True,
                            "is_primary": True,
                            "index_columns": '["order_id"]',
                        }
                    ],
                }
            ),
        },
    )

    snapshot = module._introspect(  # type: ignore[attr-defined]
        {
            "action": {
                "input": {
                    "connection_config": toolbox_postgres_config(),
                    "secret_refs": {"values": {"password": "transport-fixture-value"}},
                }
            }
        }
    )

    assert snapshot["metadata"]["resource_count"] == 1
    assert snapshot["resources"] == [
        {
            "resource_id": "orders",
            "name": "orders",
            "resource_type": "TABLE",
            "selectable": True,
            "columns": [
                {
                    "name": "order_id",
                    "data_type": "bigint",
                    "type": "bigint",
                    "source_type": "bigint",
                    "value_type": "integer",
                    "nullable": False,
                    "default_value": None,
                    "primary_key": False,
                    "primary_key_ordinal": 0,
                    "ordinal": 1,
                    "metadata": {},
                }
            ],
            "indexes": [
                {
                    "name": "PRIMARY",
                    "unique": True,
                    "columns": ["order_id"],
                    "metadata": {},
                }
            ],
            "constraints": {"primary_key": []},
            "metadata": {
                "toolbox_schema_name": "warehouse",
                "toolbox_object_type": "TABLE",
            },
        }
    ]


def test_toolbox_stdio_timeout_terminates_process(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=hang"],
    )

    _raises_input_1170_1 = toolbox_postgres_config()
    with pytest.raises(RuntimeError, match="request timed out"):
        module._invoke_toolbox(
            config=_raises_input_1170_1,
            secret_refs={"values": {"password": "transport-fixture-value"}},
            tool_name="list_tables",
            timeout_seconds=1,
        )


def test_toolbox_stdio_reports_nonzero_exit_when_poll_lags_closed_pipes() -> None:
    module = _load_main_module()

    class ExitedProcessWithLaggingPoll:
        def __init__(self) -> None:
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.close(stdout_write)
            os.write(stderr_write, b"startup failed: PASSWORD=do-not-leak")
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)

        def poll(self):
            return None

        def wait(self, timeout=None):
            assert timeout is not None
            return 1

    process = ExitedProcessWithLaggingPoll()
    state = {"bytes": 0, "stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 1
    try:
        with pytest.raises(RuntimeError, match="process failed with exit code 1") as raised:
            module._read_mcp_response(  # type: ignore[attr-defined]
                process,
                2,
                deadline,
                state,
            )
    finally:
        process.stdout.close()
        process.stderr.close()

    assert "do-not-leak" not in str(raised.value)
    assert "PASSWORD=[REDACTED]" in str(raised.value)


def test_toolbox_stdio_protocol_errors_redact_secrets(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=secret-error"],
    )

    _raises_input_1187_1 = toolbox_postgres_config()
    with pytest.raises(RuntimeError) as raised:
        module._invoke_toolbox(
            config=_raises_input_1187_1,
            secret_refs={"refs": {"password": "do-not-leak"}},
            tool_name="list_tables",
            timeout_seconds=2,
        )

    assert "do-not-leak" not in str(raised.value)
    assert "MCP tool call failed" in str(raised.value)


def test_toolbox_stdio_result_error_preserves_sanitized_diagnostic(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=result-error"],
    )

    _raises_input_1207_1 = toolbox_postgres_config()
    with pytest.raises(RuntimeError) as raised:
        module._invoke_toolbox(
            config=_raises_input_1207_1,
            secret_refs={"refs": {"password": "do-not-leak"}},
            tool_name="execute_sql",
            params={"sql": "SELECT id, FROM projects LIMIT 10"},
            timeout_seconds=2,
        )

    assert 'syntax error at or near "FROM"' in str(raised.value)
    assert "do-not-leak" not in str(raised.value)


def test_toolbox_stdio_rejects_oversized_output(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(
        module,
        "_TOOLBOX_COMMAND_OVERRIDE",
        [sys.executable, str(FAKE_TOOLBOX), "--mode=oversized"],
    )

    _raises_input_1228_1 = toolbox_postgres_config()
    with pytest.raises(RuntimeError, match="response exceeded"):
        module._invoke_toolbox(
            config=_raises_input_1228_1,
            secret_refs={"values": {"password": "transport-fixture-value"}},
            tool_name="list_tables",
            timeout_seconds=2,
        )


def test_extension_contains_no_embedded_database_or_sqlite_runtime() -> None:
    forbidden_suffixes = {".sqlite", ".sqlite3", ".db"}
    forbidden_text = "sqlite" + "3"

    assert not [
        path
        for path in EXTENSION_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in forbidden_suffixes
    ]
    assert not [
        path
        for path in EXTENSION_ROOT.rglob("*.py")
        if "tests" not in path.parts
        if forbidden_text in path.read_text(encoding="utf-8", errors="ignore")
    ]


def test_toolbox_dataset_connector_fails_closed_when_binary_absent(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.delenv("FS_EXTENSION_TEST_MODE", raising=False)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", None)
    monkeypatch.setattr(module.Path, "is_file", lambda _path: False)

    with pytest.raises(ValueError, match="Packaged Google MCP Toolbox executable"):
        module._toolbox_command({"prebuilt": "postgres"})  # type: ignore[attr-defined]


def test_toolbox_dataset_connector_prefers_real_binary_when_present(monkeypatch) -> None:
    module = _load_main_module()
    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", ["/test/toolbox"])

    command = module._toolbox_command({"prebuilt": "postgres"})  # type: ignore[attr-defined]

    assert command == ["/test/toolbox"]


def test_toolbox_dataset_connector_ignores_configured_executable(monkeypatch) -> None:
    module_path = EXTENSION_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("toolbox_dataset_connector_main", module_path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_TOOLBOX_COMMAND_OVERRIDE", ["/packaged/toolbox"])

    command = module._toolbox_command(  # type: ignore[attr-defined]
        {"prebuilt": "postgres", "toolbox_command": "sh -c touch-owned"}
    )

    assert command == ["/packaged/toolbox"]


def test_toolbox_dataset_connector_executes_saved_template_read_only() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "orders", "columns": None}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {
                        "sql": "SELECT order_id, status FROM orders WHERE status = :status"
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
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert "rows" in payload["result"]


def test_toolbox_dataset_connector_validate_query_exercises_runtime_path() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "validate_query",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "orders", "columns": None}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"}
                },
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["valid"] is True
    assert payload["result"]["row_count"] >= 0
    assert payload["result"]["output_schema"]["type"] == "object"


def test_toolbox_dataset_connector_rejects_out_of_scope_query() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "validate_query",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "customers", "columns": None}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {"sql": "SELECT order_id FROM orders LIMIT 1"}
                },
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_config"
    assert "Allowed resources: ['customers']" in payload["errors"][0]["message"]


def test_toolbox_dataset_connector_rejects_out_of_scope_columns() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {"sql": "SELECT total_amount FROM orders LIMIT 1"}
                },
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_config"
    assert "columns outside dataset scope" in payload["errors"][0]["message"]


def test_toolbox_dataset_connector_rejects_denied_column_behind_allowed_select_token() -> None:
    # Regression for FS-DEEP-CAND-046 / FS-DEEP-CAND-015: a denied column hidden
    # behind an allowed leading token (in a select expression, alias, or WHERE
    # predicate) must be rejected and must never reach execute_sql.
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "orders", "columns": ["order_id"]}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {
                        # `order_id` is allowed and leads the select item, but the
                        # expression also reads the denied `total_amount` column.
                        "sql": (
                            "SELECT order_id, total_amount AS order_id "
                            "FROM orders WHERE total_amount > 0 LIMIT 1"
                        )
                    },
                    "param_schema_jsonb": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                },
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_config"
    assert "columns outside dataset scope" in payload["errors"][0]["message"]
    assert "total_amount" in payload["errors"][0]["message"]
    # The denied column must not have leaked into any returned result rows.
    assert "rows" not in payload.get("result", {})


def test_toolbox_dataset_connector_accepts_allowed_columns_in_predicate_expressions() -> None:
    # Positive regression: a same-scope query that only references allowed
    # columns (including in WHERE / function expressions) still succeeds.
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "execute_template",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "orders", "columns": ["order_id", "status"]}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {
                        "sql": (
                            "SELECT order_id, upper(status) AS state FROM orders "
                            "WHERE status = :status ORDER BY order_id LIMIT 5"
                        )
                    },
                    "param_schema_jsonb": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                    },
                },
                "params": {"status": "open"},
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert "rows" in payload["result"]


def test_toolbox_dataset_connector_accepts_schema_qualified_scope_when_unambiguous() -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(FOR_EACH_STEP_SCOPE),
        "mode": "action",
        "action": {
            "action_id": "validate_query",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
                "dataset": {
                    "resource_scope_jsonb": {
                        "version": 1,
                        "resources": [{"resource_id": "public.orders", "columns": None}],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {"sql": "SELECT order_id FROM public.orders LIMIT 1"},
                    "param_schema_jsonb": {},
                },
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["result"]["valid"] is True


def test_toolbox_dataset_connector_normalizes_uuid_byte_arrays() -> None:
    spec = importlib.util.spec_from_file_location(
        "toolbox_dataset_connector_main", EXTENSION_ROOT / "main.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    value = module._normalize_toolbox_value(
        {"id": [18, 62, 69, 103, 137, 171, 76, 222, 128, 18, 66, 104, 174, 209, 136, 22]}
    )

    assert value == {"id": "123e4567-89ab-4cde-8012-4268aed18816"}


# ---------------------------------------------------------------------------
# The runtime row limit is the host's, not this extension's
# ---------------------------------------------------------------------------


def test_runtime_limit_follows_the_host_approved_value() -> None:
    """The host approves `row_limit` against FS_DATASET_QUERY_MAX_RUNTIME_ROWS.

    This extension used to clamp `max_runtime_rows` to its own hard-coded 1000, so
    raising the deployment maximum silently did nothing. `maximum=None` means the
    ceiling belongs to the host and re-clamping here would ignore an operator's
    configuration.
    """
    module = _runtime_module()
    request = {"action": {"input": {"limits": {"max_runtime_rows": 10000}}}}

    assert module._limit(request, key="max_runtime_rows", default=100, maximum=None) == 10000


def test_preview_limit_is_still_owned_by_this_extension() -> None:
    """Preview stays capped at 100 rows regardless of the deployment maximum."""
    module = _runtime_module()
    request = {"action": {"input": {"limits": {"max_preview_rows": 10000}}}}

    assert module._limit(request, key="max_preview_rows", default=10, maximum=100) == 100


def test_an_absent_limit_falls_back_to_the_default() -> None:
    module = _runtime_module()
    assert (
        module._limit({"action": {"input": {}}}, key="max_runtime_rows", default=100, maximum=None)
        == 100
    )


@pytest.mark.parametrize("scope", [ROOT_STEP_SCOPE, GROUP_STEP_SCOPE, FOR_EACH_STEP_SCOPE])
def test_every_final_scope_owner_introspects_through_the_entrypoint(scope) -> None:
    request = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(scope),
        "mode": "action",
        "action": {
            "action_id": "introspect_schema",
            "input": {
                "connection_config": toolbox_postgres_config(),
                "secret_refs": {"values": {"password": "transport-fixture-value"}},
            },
        },
    }
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert "orders" in {row["resource_id"] for row in payload["result"]["resources"]}


@pytest.mark.parametrize(
    ("contract_version", "scope", "reason"),
    [
        ("extension_host_v1", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        ("", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        (
            "extension_host_v2",
            {k: v for k, v in FOR_EACH_STEP_SCOPE.items() if k != "for_each_id"},
            "misses execution scope",
        ),
        (
            "extension_host_v2",
            {**FOR_EACH_STEP_SCOPE, "group_id": "load_sources"},
            "both a Group and a For each",
        ),
        (
            "extension_host_v2",
            {**ROOT_STEP_SCOPE, "step_execution_id": None},
            "without its logical execution",
        ),
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        {"mode": "action", "action": {"action_id": "test_connection", "input": {}}},
        {"mode": "action", "action": {"action_id": "introspect_schema", "input": {}}},
        {"mode": "query", "query": {"query_id": "runtime_health"}},
    ],
)
def test_foreign_or_incomplete_envelope_is_refused_before_toolbox_starts(
    monkeypatch, operation, contract_version, scope, reason
) -> None:
    module = _runtime_module()

    def no_toolbox(*_args, **_kwargs):
        raise AssertionError("a refused envelope must not start Toolbox or reach a database")

    monkeypatch.setattr(module, "_invoke_toolbox", no_toolbox)
    monkeypatch.setattr(module.subprocess, "Popen", no_toolbox)
    request = {"contract_version": contract_version, "runtime_context": dict(scope), **operation}
    if operation["mode"] == "action":
        request["action"]["input"] = {
            "connection_config": toolbox_postgres_config(),
            "secret_refs": {"values": {"password": "transport-fixture-value"}},
        }

    response = module.handle_request(request)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_host_envelope"
    assert reason in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert response["errors"][0]["code"] == "invalid_host_envelope"


def test_the_entrypoint_refuses_a_request_without_the_host_contract() -> None:
    completed = subprocess.run(  # nosemgrep: fixed Python executable and extension runtime path. # nosec B603
        [sys.executable, str(TEST_RUNTIME)],
        input=json.dumps({"mode": "query", "query": {"query_id": "runtime_health"}}),
        text=True,
        capture_output=True,
        check=False,
        env=toolbox_env(),
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["error_code"] == "invalid_host_envelope"
    assert payload["definitely_no_external_effect"] is True


def test_logo_is_a_square_png_within_the_host_limits() -> None:
    # Core requires icon.png at the bundle root: PNG, square, 32-512 px, <= 100 KB.
    data = (EXTENSION_ROOT / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height
    assert 32 <= width <= 512
    assert len(data) <= 100 * 1024
