"""Credentials follow the actual pinned profile, before any provider dispatch."""

from __future__ import annotations

import pytest
from test_toolbox_dataset_connector import _runtime_module, OFFERED_PREBUILTS
from test_sql_writes import request

REQUIRED = sorted(OFFERED_PREBUILTS & {"postgres", "mysql", "mariadb", "mssql", "cloud-sql-mssql"})
OPTIONAL = sorted(OFFERED_PREBUILTS - set(REQUIRED))


@pytest.mark.parametrize("profile", REQUIRED)
@pytest.mark.parametrize("test_mode", [False, True])
def test_missing_write_password_never_invokes_provider(monkeypatch, profile, test_mode):
    module = _runtime_module()
    calls = []
    monkeypatch.setattr(module, "_invoke_toolbox", lambda *args, **kwargs: calls.append(args))
    payload = request(profile)
    payload["runtime_context"]["test_mode"] = test_mode
    payload["action"]["target"]["connection"]["secrets"] = {}
    result = module.handle_request(payload)
    assert result["ok"] is False
    assert result["definitely_no_external_effect"] is True
    assert "requires password" in result["error"]
    assert calls == []


@pytest.mark.parametrize("profile", REQUIRED)
def test_missing_read_password_never_starts_driver(monkeypatch, profile):
    module = _runtime_module()
    calls = []
    monkeypatch.setattr(module, "_toolbox_command", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="requires password"):
        module._invoke_toolbox({"prebuilt": profile}, {}, "list_tables")
    assert calls == []


@pytest.mark.parametrize("profile", REQUIRED)
def test_secret_field_and_profile_env_are_supported(profile):
    module = _runtime_module()
    config = {"prebuilt": profile}
    runtime_profile = module._runtime_prebuilt(config)
    variable = next(
        k for k, v in module.TOOLBOX_ENV_FIELDS[runtime_profile].items() if v == "password"
    )
    for secrets in [
        {"values": {"password": "credential-fixture-value"}},
        {"values": {"env": {variable: "credential-fixture-value"}}},
    ]:
        module._validate_connection_credentials(config, secrets)
        assert module._toolbox_env(config, secrets)[variable] == "credential-fixture-value"
    with pytest.raises(ValueError, match="requires password"):
        module._validate_connection_credentials(
            config, {"values": {"env": {"WRONG_PROFILE_PASSWORD": "credential-fixture-value"}}}
        )
    with pytest.raises(ValueError, match="requires password"):
        module._validate_connection_credentials(config, {"values": {"password": " "}})


@pytest.mark.parametrize("profile", OPTIONAL)
def test_managed_iam_or_native_optional_password_remains_optional(profile):
    module = _runtime_module()
    module._validate_connection_credentials({"prebuilt": profile}, {})
