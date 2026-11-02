"""Raw dollar delimiters must never hide a second statement in the write path."""

import importlib.util
import json
from pathlib import Path

import pytest

import sql_writes

PAYLOADS = [
    "UPDATE products SET x$$=1; DELETE FROM products; UPDATE products SET x$$=2 WHERE sku='a'",
    "UPDATE products SET x$tag$=1; DROP TABLE products; UPDATE products SET x$tag$=2 WHERE sku='a'",
]


@pytest.mark.parametrize("sql", PAYLOADS)
def test_write_statement_rejects_dollar_delimiters_before_dialect_scan(sql):
    with pytest.raises(ValueError, match="dollar-quote"):
        sql_writes.write_statement(sql, prebuilt="postgres")


@pytest.mark.parametrize("sql", PAYLOADS)
@pytest.mark.parametrize("mysql,postgres", [(False, True), (True, False), (False, False)])
def test_direct_write_lexer_rejects_dollar_delimiters(sql, mysql, postgres):
    with pytest.raises(ValueError, match="dollar-quote"):
        sql_writes._write_tokens(sql, mysql=mysql, postgres=postgres)


def load_runtime():
    root = Path(__file__).resolve().parents[1]
    path = root / ("sql_runtime.py" if (root / "sql_runtime.py").exists() else "main.py")
    spec = importlib.util.spec_from_file_location("write_guard_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("sql", PAYLOADS)
@pytest.mark.parametrize("test_mode", [False, True])
def test_dollar_write_never_invokes_provider_even_with_claim(tmp_path, monkeypatch, sql, test_mode):
    module = load_runtime()
    offered = module.TOOLBOX_DATASET_PREBUILTS & sql_writes.WRITE_PREBUILTS
    if not offered:
        pytest.skip("Legacy read-only variant does not expose workflow writes")
    prebuilt = sorted(offered)[0]
    connection_type = "toolbox_sql_writer"
    config = {"prebuilt": prebuilt}
    if prebuilt == "sqlite":
        connection_type = "sqlite"
        config["database"] = "guard.db"
        monkeypatch.setattr(module, "SQLITE_STORAGE_ROOT", tmp_path / "owned")
    monkeypatch.setattr(
        module, "_invoke_toolbox", lambda *a, **k: pytest.fail("script reached provider")
    )
    payload = {
        "runtime_context": {
            "account_id": "account-1",
            "project_id": "project-1",
            "external_effect_id": "claimed-effect",
            "test_mode": test_mode,
        },
        "action": {
            "input": {"connection_ref": "database-1", "sql": sql},
            "target": {
                "connection": {
                    "connection_id": "database-1",
                    "connection_type": connection_type,
                    "config": config,
                    "secrets": {},
                }
            },
        },
    }
    response = module._execute_write(payload)
    assert response["ok"] is False
    assert response["definitely_no_external_effect"] is True
    assert "dollar-quote" in response["error"]


def test_write_bound_values_keep_dollar_and_backslash_data():
    module = load_runtime()
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    sql, occurrences = sql_writes.write_statement(
        "UPDATE products SET name=:value", prebuilt=prebuilt
    )
    value = r"C:\folder $$ $tag$"
    bound = module._compile_bound_query(prebuilt, sql, {"value": value}, occurrences=occurrences)
    assert json.loads(bound["params_json"]) == {"value": value}
    assert value not in bound["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE products SET name=$тэг$'$тэг$; DELETE FROM products; -- ' WHERE sku='a'",
        "UPDATE products SET name=$💣$'$💣$; DELETE FROM products; -- ' WHERE sku='a'",
        "UPDATE products SET name='$' WHERE sku='a'",
        "UPDATE products SET price$usd=1 WHERE sku='a'",
    ],
)
def test_every_raw_dollar_is_rejected_before_write_parsing(sql):
    for call in [
        lambda value: sql_writes.write_statement(value, prebuilt="postgres"),
        lambda value: sql_writes._write_tokens(value, mysql=False, postgres=True),
    ]:
        with pytest.raises(ValueError, match="raw dollar"):
            call(sql)


def test_unicode_dollar_data_stays_separate_from_write_sql():
    module = load_runtime()
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    sql, occurrences = sql_writes.write_statement(
        "UPDATE products SET name=:value", prebuilt=prebuilt
    )
    value = "$тэг$ $💣$ $"
    bound = module._compile_bound_query(prebuilt, sql, {"value": value}, occurrences=occurrences)
    assert json.loads(bound["params_json"]) == {"value": value}
    assert value not in bound["sql"]
