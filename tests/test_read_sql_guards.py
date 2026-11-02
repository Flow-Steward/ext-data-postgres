"""Read validation must reject dialect-ambiguous escapes and unbounded LIMITs."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def guarded_runtime():
    root = Path(__file__).resolve().parents[1]
    path = root / ("sql_runtime.py" if (root / "sql_runtime.py").exists() else "main.py")
    spec = importlib.util.spec_from_file_location("read_guard_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT '\'; DELETE FROM products; -- '",
        r"SELECT '\', pg_sleep(30) -- '",
        r"SELECT '\' LIMIT 1",
        r"SELECT 1 -- \\ ignored",
    ],
)
def test_backslash_is_rejected_before_every_read_lexer_or_rewrite(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    calls = [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="backslash"):
            call(sql)


@pytest.mark.parametrize(
    "clause",
    [
        "LIMIT 1+1000000",
        "LIMIT (1+1000000)",
        "LIMIT 1,1000000",
        "LIMIT 1 OFFSET 0+1000000",
        "LIMIT 1 OFFSET (2)",
        "LIMIT 1 UNION SELECT sku FROM products",
    ],
)
def test_limit_is_a_complete_literal_clause(guarded_runtime, clause):
    module = guarded_runtime
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS - {"mssql", "cloud-sql-mssql"}:
        with pytest.raises(ValueError):
            module._limited_sql(prebuilt, "SELECT sku FROM products " + clause, 100)


def test_literal_offset_and_nested_limits_keep_outer_cap(guarded_runtime):
    module = guarded_runtime
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS - {"mssql", "cloud-sql-mssql"}:
        assert module._limited_sql(
            prebuilt, "SELECT sku FROM products LIMIT 1 OFFSET 2", 100
        ).endswith("LIMIT 1 OFFSET 2")
        assert module._limited_sql(
            prebuilt, "SELECT * FROM (SELECT sku FROM products LIMIT 1+1000000) AS nested", 100
        ).endswith("AS nested LIMIT 100")
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS & {"mssql", "cloud-sql-mssql"}:
        with pytest.raises(ValueError, match="set queries"):
            module._limited_sql(
                prebuilt, "SELECT sku FROM products UNION SELECT sku FROM products", 100
            )


@pytest.mark.parametrize("test_mode", [False, True])
def test_unsafe_read_never_reaches_provider_even_without_effect_claim(
    guarded_runtime, monkeypatch, test_mode
):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    monkeypatch.setattr(module, "_connection_config", lambda request: {"prebuilt": prebuilt})
    monkeypatch.setattr(
        module,
        "_query_definition",
        lambda request: {"sql": r"SELECT '\'; DELETE FROM products; -- '"},
    )
    monkeypatch.setattr(
        module, "_invoke_toolbox", lambda *a, **k: pytest.fail("unsafe read reached a driver")
    )
    with pytest.raises(ValueError, match="backslash"):
        module._execute_sql(
            {"runtime_context": {"test_mode": test_mode}, "action": {"input": {}}}, preview=False
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x$$ FROM products; DELETE FROM products; SELECT x$$ FROM products WHERE sku='a'",
        "SELECT x$tag$ FROM products; DELETE FROM products; SELECT x$tag$ FROM products WHERE sku='a'",
    ],
)
def test_dollar_quote_confusion_is_rejected_before_all_read_entrypoints(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    calls = [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="dollar-quote"):
            call(sql)


@pytest.mark.parametrize("test_mode", [False, True])
def test_dollar_script_never_invokes_provider_without_write_claim(
    guarded_runtime, monkeypatch, test_mode
):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    monkeypatch.setattr(module, "_connection_config", lambda request: {"prebuilt": prebuilt})
    monkeypatch.setattr(
        module,
        "_query_definition",
        lambda request: {
            "sql": "SELECT x$$ FROM products; DELETE FROM products; SELECT x$$ FROM products WHERE sku='a'"
        },
    )
    monkeypatch.setattr(
        module,
        "_invoke_toolbox",
        lambda *a, **k: pytest.fail("read script reached provider without a write claim"),
    )
    with pytest.raises(ValueError, match="dollar-quote"):
        module._execute_sql(
            {"runtime_context": {"test_mode": test_mode}, "action": {"input": {}}}, preview=False
        )


def test_bound_values_may_contain_backslashes_and_dollar_text(guarded_runtime):
    module = guarded_runtime
    import json

    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS:
        value = r"C:\folder $$ $tag$"
        bound = module._compile_bound_query(prebuilt, "SELECT :value AS value", {"value": value})
        assert json.loads(bound["params_json"]) == {"value": value}
        assert value not in bound["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $тэг$'$тэг$; DELETE FROM products; -- ' FROM products WHERE sku='a'",
        "SELECT $💣$'$💣$; DELETE FROM products; -- ' FROM products WHERE sku='a'",
        "SELECT '$' AS value LIMIT 1",
        "SELECT price$usd FROM products LIMIT 1",
    ],
)
def test_every_raw_dollar_is_rejected_before_read_parsing(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    for call in [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]:
        with pytest.raises(ValueError, match="raw dollar"):
            call(sql)


def test_generated_numbered_placeholders_and_unicode_dollar_values_remain_valid(guarded_runtime):
    module = guarded_runtime
    import json

    value = "$тэг$ $💣$ $"
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS:
        bound = module._compile_bound_query(prebuilt, "SELECT :value AS value", {"value": value})
        assert json.loads(bound["params_json"]) == {"value": value}
        assert value not in bound["sql"]
