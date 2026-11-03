#!/usr/bin/env python3
"""PostgreSQL dataset reads and parameterized workflow writes."""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import re
import selectors
import signal
import subprocess  # nosec B404
import sys
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from flowsteward_extension_sdk import runtime_context_from_payload

from sql_writes import write_input, write_statement
from vector_commands import FIELDS as VECTOR_ACTIONS
from vector_runtime import handle as handle_vector_action

_MESSAGE_ONLY_READ_ONLY_SELECT_STATEMENTS_ARE_ALLOWED = (
    "Only read-only SELECT statements are allowed"
)
_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES = (
    "Could not confidently identify referenced resources"
)

EXTENSION_ID = "flowsteward.data-connector-mcp-toolbox-postgres"
_TOOLBOX_COMMAND_OVERRIDE: list[str] | None = None
_MAX_MCP_OUTPUT_BYTES = 1024 * 1024
TOOLBOX_SOURCE_CATALOG = {
    "alloydb-omni": {"display_name": "AlloyDB Omni", "query_language": "sql"},
    "alloydb-postgres": {"display_name": "AlloyDB for PostgreSQL", "query_language": "sql"},
    "cloud-sql-postgres": {"display_name": "Cloud SQL for PostgreSQL", "query_language": "sql"},
    "postgres": {"display_name": "Postgres", "query_language": "sql"},
}


def _variant() -> dict[str, Any]:
    """Load the fixed connection profiles compiled into this database package."""
    path = Path(__file__).with_name("variant.json")
    variant = json.loads(path.read_text(encoding="utf-8"))
    if set(variant.get("prebuilts") or ()) != set(TOOLBOX_SOURCE_CATALOG):
        raise RuntimeError("variant.json must match this database's compiled profiles")
    return variant


_VARIANT = _variant()
TOOLBOX_DATASET_PREBUILTS = frozenset(_VARIANT.get("prebuilts") or TOOLBOX_SOURCE_CATALOG)
EXTENSION_DISPLAY_NAME = str(_VARIANT.get("display_name") or "Toolbox Dataset Connector")
# Toolbox environment variable -> connection field name. The "password" values
# name a field; they are not secrets.
_AUTH_FIELD = "password"
TOOLBOX_ENV_FIELDS = {
    "alloydb-omni": {
        "ALLOYDB_OMNI_HOST": "host",
        "ALLOYDB_OMNI_PORT": "port",
        "ALLOYDB_OMNI_DATABASE": "database",
        "ALLOYDB_OMNI_USER": "user",
        "ALLOYDB_OMNI_PASSWORD": _AUTH_FIELD,
        "ALLOYDB_OMNI_QUERY_PARAMS": "query_params",
    },
    "alloydb-postgres": {
        "ALLOYDB_POSTGRES_PROJECT": "project",
        "ALLOYDB_POSTGRES_REGION": "region",
        "ALLOYDB_POSTGRES_CLUSTER": "cluster",
        "ALLOYDB_POSTGRES_INSTANCE": "instance",
        "ALLOYDB_POSTGRES_DATABASE": "database",
        "ALLOYDB_POSTGRES_USER": "user",
        "ALLOYDB_POSTGRES_PASSWORD": _AUTH_FIELD,
        "ALLOYDB_POSTGRES_IP_TYPE": "ip_type",
    },
    "cloud-sql-postgres": {
        "CLOUD_SQL_POSTGRES_PROJECT": "project",
        "CLOUD_SQL_POSTGRES_REGION": "region",
        "CLOUD_SQL_POSTGRES_INSTANCE": "instance",
        "CLOUD_SQL_POSTGRES_DATABASE": "database",
        "CLOUD_SQL_POSTGRES_USER": "user",
        "CLOUD_SQL_POSTGRES_PASSWORD": _AUTH_FIELD,
        "CLOUD_SQL_POSTGRES_IP_TYPE": "ip_type",
    },
    "postgres": {
        "POSTGRES_HOST": "host",
        "POSTGRES_PORT": "port",
        "POSTGRES_DATABASE": "database",
        "POSTGRES_USER": "user",
        "POSTGRES_PASSWORD": _AUTH_FIELD,
        "POSTGRES_QUERY_PARAMS": "query_params",
    },
}
TOOLBOX_RUNTIME_PREBUILT_ALIASES: dict[str, str] = {}


def _connector_type_id(prebuilt: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", prebuilt.lower()).strip("_")
    return f"toolbox_{suffix}"


def _supported_connector_rows() -> list[dict[str, Any]]:
    return [
        {
            "connector_id": _connector_type_id(prebuilt),
            "connector_type_id": _connector_type_id(prebuilt),
            "database_kind": prebuilt,
            "display_name": metadata["display_name"],
            "source_kind": prebuilt,
            "source_extension_id": EXTENSION_ID,
            "source_extension_name": EXTENSION_DISPLAY_NAME,
            "source_provider": EXTENSION_DISPLAY_NAME,
            "query_language": metadata["query_language"],
            "access_mode": "read_only",
            "supported_prebuilts": prebuilt,
        }
        for prebuilt, metadata in sorted(
            (
                (source_kind, metadata)
                for source_kind, metadata in TOOLBOX_SOURCE_CATALOG.items()
                if source_kind in TOOLBOX_DATASET_PREBUILTS
            ),
            key=lambda item: item[1]["display_name"].lower(),
        )
    ]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _toolbox_rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("rows", "items", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        return [value]
    return []


def _envelope(
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
    errors: list[dict[str, str]] | None = None,
    reload: list[str] | None = None,
) -> dict[str, Any]:
    normalized_errors = list(errors or [])
    payload = {
        "ok": ok,
        "result": result or {},
        "commands": [],
        "events": [],
        "reload": reload or [],
        "errors": normalized_errors,
    }
    if not ok and normalized_errors:
        first_error = normalized_errors[0]
        payload["error_code"] = str(first_error.get("code") or "connector_runtime_failed")
        payload["error"] = str(first_error.get("message") or "Extension request failed")
    return payload


def _error(code: str, message: str, *, path: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def _action_input(request: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_as_dict(request.get("action")).get("input"))


def _connection_config(request: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_action_input(request).get("connection_config"))


def _secret_refs(request: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(_action_input(request).get("secret_refs"))


def _secret_ref_values(secret_refs: dict[str, Any]) -> dict[str, Any]:
    refs = _as_dict(secret_refs.get("refs"))
    values = _as_dict(secret_refs.get("values"))
    if values:
        return values
    if refs:
        return refs
    return _as_dict(secret_refs)


def _prebuilt(config: dict[str, Any]) -> str:
    prebuilt = str(
        config.get("prebuilt") or config.get("source_kind") or config.get("source_type") or ""
    ).strip()
    if not prebuilt:
        raise ValueError("connection_config.prebuilt is required")
    if prebuilt not in TOOLBOX_DATASET_PREBUILTS:
        raise ValueError(
            f"Google MCP Toolbox source '{prebuilt}' is not a supported dataset connector"
        )
    return prebuilt


def _runtime_prebuilt(config: dict[str, Any]) -> str:
    prebuilt = _prebuilt(config)
    return TOOLBOX_RUNTIME_PREBUILT_ALIASES.get(prebuilt, prebuilt)


def _toolbox_command(_config: dict[str, Any]) -> list[str]:
    if _TOOLBOX_COMMAND_OVERRIDE:
        return list(_TOOLBOX_COMMAND_OVERRIDE)
    machines = {
        "x86_64": "linux-amd64",
        "amd64": "linux-amd64",
        "aarch64": "linux-arm64",
        "arm64": "linux-arm64",
    }
    target = (
        machines.get(platform.machine().strip().lower()) if platform.system() == "Linux" else None
    )
    if not target:
        raise ValueError("Packaged Google MCP Toolbox supports only Linux amd64 and arm64")
    binary = Path(__file__).resolve().parent / "bin" / target / "toolbox"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"Packaged Google MCP Toolbox executable is unavailable for {target}")
    return [str(binary)]


_PASSWORD_REQUIRED_PROFILES = frozenset(["postgres"])


def _validate_connection_credentials(config: dict[str, Any], secret_refs: dict[str, Any]) -> None:
    """Reject missing native credentials before invoking a database provider."""
    profile = _prebuilt(config)
    if profile not in _PASSWORD_REQUIRED_PROFILES:
        return
    runtime_profile = _runtime_prebuilt(config)
    fields = TOOLBOX_ENV_FIELDS[runtime_profile]
    env_name = next(key for key, field in fields.items() if field == _AUTH_FIELD)
    values = _secret_ref_values(secret_refs)
    secret_env = {**_as_dict(secret_refs.get("env")), **_as_dict(values.get("env"))}
    value = secret_env.get(env_name)
    if value is None:
        value = values.get(_AUTH_FIELD, values.get(env_name))
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Profile {profile} requires password in connection secrets or secret env {env_name}"
        )


def _toolbox_env(config: dict[str, Any], secret_refs: dict[str, Any]) -> dict[str, str]:
    prebuilt = _runtime_prebuilt(config)
    env = dict(os.environ)
    explicit_env = _as_dict(config.get("env"))
    secret_env = _as_dict(secret_refs.get("env"))
    secrets = _secret_ref_values(secret_refs)
    secret_env = {**secret_env, **_as_dict(secrets.get("env"))}
    for env_name, config_key in TOOLBOX_ENV_FIELDS.get(prebuilt, {}).items():
        value = explicit_env.get(env_name)
        if value is None:
            value = config.get(config_key)
        if value is None:
            value = secrets.get(config_key) or secrets.get(env_name)
        if value is not None and str(value) != "":
            env[env_name] = str(value)
    for env_name, value in explicit_env.items():
        if value is not None and str(value) != "":
            env[str(env_name)] = str(value)
    for env_name, value in secret_env.items():
        if value is not None and str(value) != "":
            env[str(env_name)] = str(value)
    return env


def _json_from_toolbox_output(output: str) -> Any:
    text = output.strip()
    if not text:
        return None
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            return json.loads(text[index:])
        except json.JSONDecodeError:
            continue
    raise ValueError("Google MCP Toolbox returned non-JSON output")


def _invoke_toolbox(
    config: dict[str, Any],
    secret_refs: dict[str, Any],
    tool_name: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: int = 30,
) -> Any:
    _validate_connection_credentials(config, secret_refs)
    prebuilt = _runtime_prebuilt(config)
    command = [*_toolbox_command(config), f"--prebuilt={prebuilt}", "--stdio"]
    process = subprocess.Popen(  # nosemgrep: fixed packaged argv, never shell=True. # nosec B603
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_toolbox_env(config, secret_refs),
        start_new_session=True,
    )
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    output_state = {"bytes": 0, "stdout": bytearray(), "stderr": bytearray()}
    try:
        _send_mcp_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": EXTENSION_ID, "version": "1.4.1"},
                },
            },
        )
        initialized = _read_mcp_response(process, 1, deadline, output_state)
        if isinstance(initialized.get("error"), dict):
            raise RuntimeError("Google MCP Toolbox MCP initialize failed")
        _send_mcp_message(
            process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        _send_mcp_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": dict(params or {})},
            },
        )
        response = _read_mcp_response(process, 2, deadline, output_state)
    finally:
        _stop_toolbox_process(process)
    if isinstance(response.get("error"), dict):
        error = _as_dict(response["error"])
        code = error.get("code")
        suffix = f" (code {code})" if isinstance(code, int) else ""
        raise RuntimeError(f"Google MCP Toolbox MCP tool call failed{suffix}")
    result = _as_dict(response.get("result"))
    if result.get("isError"):
        diagnostics: list[str] = []
        secret_values = {
            str(value) for value in _secret_ref_values(secret_refs).values() if str(value or "")
        }
        for content in _as_list(result.get("content")):
            if not isinstance(content, dict) or str(content.get("type") or "") != "text":
                continue
            diagnostic = _redact(str(content.get("text") or "").strip())
            for secret_value in secret_values:
                diagnostic = diagnostic.replace(secret_value, "[REDACTED]")
            if diagnostic:
                diagnostics.append(diagnostic)
        detail = " ".join(diagnostics)[:512]
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Google MCP Toolbox MCP tool call failed{suffix}")
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    parsed_content: list[Any] = []
    parsed_any_content = False
    parse_error: ValueError | None = None
    for content in _as_list(result.get("content")):
        if isinstance(content, dict) and str(content.get("type") or "") == "text":
            try:
                parsed = _json_from_toolbox_output(str(content.get("text") or ""))
            except ValueError as exc:
                parse_error = exc
                continue
            parsed_any_content = True
            if isinstance(parsed, list):
                parsed_content.extend(parsed)
            else:
                parsed_content.append(parsed)
    if parsed_content:
        return parsed_content[0] if len(parsed_content) == 1 else parsed_content
    if parse_error is not None:
        raise parse_error
    if parsed_any_content or result.get("content") == []:
        return []
    return result


def _send_mcp_message(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("Google MCP Toolbox stdin is unavailable")
    try:
        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        process.stdin.flush()
    except OSError as exc:
        raise RuntimeError("Google MCP Toolbox process closed its input") from exc


def _read_mcp_response(
    process: subprocess.Popen[bytes],
    request_id: int,
    deadline: float,
    state: dict[str, Any],
) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Google MCP Toolbox output pipes are unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Google MCP Toolbox request timed out")
            events = selector.select(remaining)
            if not events:
                raise RuntimeError("Google MCP Toolbox request timed out")
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                state["bytes"] = int(state["bytes"]) + len(chunk)
                if int(state["bytes"]) > _MAX_MCP_OUTPUT_BYTES:
                    raise RuntimeError(
                        f"Google MCP Toolbox response exceeded {_MAX_MCP_OUTPUT_BYTES} bytes"
                    )
                if key.data == "stderr":
                    state["stderr"].extend(chunk)
                    continue
                if key.data != "stdout":
                    continue
                stdout = state["stdout"]
                stdout.extend(chunk)
                while b"\n" in stdout:
                    raw_line, _, tail = stdout.partition(b"\n")
                    state["stdout"] = stdout = bytearray(tail)
                    try:
                        payload = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict) and payload.get("id") == request_id:
                        return payload
            if process.poll() is not None and not selector.get_map():
                break
    finally:
        selector.close()
    code = process.poll()
    if code is None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                code = process.wait(timeout=min(1.0, remaining))
            except subprocess.TimeoutExpired:
                code = process.poll()
    if code not in (None, 0):
        stderr = bytes(state["stderr"]).decode("utf-8", errors="replace").strip()
        details = _redact(stderr)[:512]
        suffix = f": {details}" if details else ""
        raise RuntimeError(f"Google MCP Toolbox process failed with exit code {code}{suffix}")
    raise RuntimeError("Google MCP Toolbox returned no matching JSON-RPC response")


def _stop_toolbox_process(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _redact(value: str) -> str:
    text = str(value)
    for key in ("PASSWORD", "TOKEN", "SECRET", "KEY"):
        text = re.sub(rf"({key}[^=\n]*=)([^\s]+)", r"\1[REDACTED]", text, flags=re.I)
    return text


def _column_type(raw_type: str) -> str:
    normalized = str(raw_type or "").strip().upper()
    if "INT" in normalized:
        return "integer"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "number"
    if "BOOL" in normalized:
        return "boolean"
    if any(token in normalized for token in ("DATE", "TIME")):
        return "datetime"
    if "BLOB" in normalized or "BYTE" in normalized:
        return "binary"
    return "string"


def _normalize_toolbox_resource(row: dict[str, Any]) -> dict[str, Any]:
    details = _json_object(row.get("object_details")) or row
    resource_name = str(
        details.get("object_name") or details.get("table_name") or details.get("name") or ""
    ).strip()
    resource_type = str(details.get("object_type") or details.get("type") or "table").strip()
    columns: list[dict[str, Any]] = []
    primary_key_columns: list[str] = []
    for index, column in enumerate(_as_list(details.get("columns"))):
        if not isinstance(column, dict):
            continue
        name = str(column.get("column_name") or column.get("name") or "").strip()
        if not name:
            continue
        source_type = str(column.get("data_type") or column.get("source_type") or "").strip()
        primary_key = bool(column.get("is_primary_key") or column.get("primary_key"))
        if primary_key:
            primary_key_columns.append(name)
        columns.append(
            {
                "name": name,
                "data_type": source_type,
                "type": source_type,
                "source_type": source_type,
                "value_type": _column_type(source_type),
                "nullable": not bool(column.get("is_not_nullable") or column.get("notnull")),
                "default_value": column.get("column_default") or column.get("default_value"),
                "primary_key": primary_key,
                "primary_key_ordinal": 1 if primary_key else 0,
                "ordinal": int(column.get("ordinal_position") or index),
                "metadata": {},
            }
        )
    indexes = []
    for index in _as_list(details.get("indexes")):
        if isinstance(index, dict):
            indexes.append(
                {
                    "name": str(index.get("index_name") or index.get("name") or "").strip(),
                    "unique": bool(index.get("is_unique") or index.get("unique")),
                    "columns": [
                        str(item)
                        for item in _json_list(index.get("columns") or index.get("index_columns"))
                    ],
                    "metadata": {},
                }
            )
    constraints = {"primary_key": primary_key_columns}
    if isinstance(details.get("constraints"), list):
        constraints["raw"] = details["constraints"]
    return {
        "resource_id": resource_name,
        "name": resource_name,
        "resource_type": resource_type,
        "selectable": True,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "metadata": {
            "toolbox_schema_name": details.get("schema_name"),
            "toolbox_object_type": details.get("object_type"),
        },
    }


def _introspect(request: dict[str, Any]) -> dict[str, Any]:
    config = _connection_config(request)
    prebuilt = _prebuilt(config)
    tables = _invoke_toolbox(
        config,
        _secret_refs(request),
        "list_tables",
        timeout_seconds=_limit(request, key="timeout_seconds", default=30, maximum=120),
    )
    resources = [
        resource
        for row in _toolbox_rows(tables)
        if isinstance(row, dict)
        and (resource := _normalize_toolbox_resource(row)).get("resource_id")
    ]
    return {
        "schema_version": "dataset_schema_snapshot.v1",
        "source_kind": prebuilt,
        "resources": resources,
        "metadata": {
            "connector_id": _connector_type_id(prebuilt),
            "toolbox_prebuilt": prebuilt,
            "resource_count": len(resources),
        },
    }


def _exception_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "invalid_config"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    return "connector_runtime_failed"


def _query_definition(request: dict[str, Any]) -> dict[str, Any]:
    template = _as_dict(_action_input(request).get("query_template"))
    return _as_dict(template.get("query_definition_jsonb") or template.get("query_definition"))


def _param_schema(request: dict[str, Any]) -> dict[str, Any]:
    template = _as_dict(_action_input(request).get("query_template"))
    return _as_dict(template.get("param_schema_jsonb") or template.get("param_schema"))


def _dataset_scope(request: dict[str, Any]) -> dict[str, set[str] | None]:
    dataset = _as_dict(_action_input(request).get("dataset"))
    scope = _as_dict(dataset.get("resource_scope_jsonb") or dataset.get("resource_scope"))
    raw_resources = scope.get("resources")
    resources = raw_resources if isinstance(raw_resources, list) else []
    allowed: dict[str, set[str] | None] = {}
    short_name_map: dict[str, list[tuple[str, set[str] | None]]] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_id = str(resource.get("resource_id") or "").strip().lower()
        if not resource_id:
            continue
        columns = resource.get("columns")
        allowed_columns = (
            None
            if columns is None
            else {str(column or "").strip() for column in columns if str(column or "").strip()}
        )
        allowed[resource_id] = allowed_columns
        short_name_map.setdefault(_resource_key(resource_id), []).append(
            (resource_id, allowed_columns)
        )
    for short_name, matches in short_name_map.items():
        if short_name in allowed or len(matches) != 1:
            continue
        allowed[short_name] = matches[0][1]
    return allowed


_PARAM_REFS = re.compile(r":([A-Za-z_](?a:\w*))")
_PARAMETER_BINDERS = {
    "alloydb-omni": "numbered",
    "alloydb-postgres": "numbered",
    "cloud-sql-postgres": "numbered",
    "postgres": "numbered",
}
# Strips single/double-quoted string literals so their contents are never
# mistaken for column identifiers during scope analysis.
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
# Matches an (optionally dotted/quoted) identifier reference. Quoted identifiers
# ("col", `col`, [col]) are normalized to their inner name by _bare_identifier.
_IDENTIFIER_REF = re.compile(
    r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_]\w*)'
    r'(?:\s*\.\s*(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_]\w*|\*))*'
)
_READ_ONLY_FORBIDDEN_TOKENS = frozenset(
    {
        "alter",
        "analyze",
        "attach",
        "begin",
        "call",
        "commit",
        "copy",
        "create",
        "delete",
        "detach",
        "drop",
        "execute",
        "exec",
        "grant",
        "insert",
        "into",
        "load",
        "lock",
        "merge",
        "pragma",
        "refresh",
        "replace",
        "revoke",
        "rollback",
        "set",
        "share",
        "truncate",
        "unload",
        "update",
        "use",
        "vacuum",
    }
)
_SOURCE_CLAUSE_TERMINATORS = frozenset(
    {
        "where",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "fetch",
        "union",
        "intersect",
        "except",
        "window",
        "qualify",
        "on",
    }
)
_READ_ONLY_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "cast",
        "ceil",
        "ceiling",
        "coalesce",
        "concat",
        "count",
        "date_trunc",
        "extract",
        "floor",
        "greatest",
        "least",
        "length",
        "lower",
        "ltrim",
        "max",
        "min",
        "nullif",
        "round",
        "row_number",
        "rtrim",
        "substring",
        "sum",
        "trim",
        "upper",
    }
)


def _sql_tokens(sql: str) -> list[tuple[str, str]]:
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    """Tokenize SQL code while deliberately excluding literal/comment contents."""
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if char.isspace():
            index += 1
            continue
        if char == "-" and following == "-":
            end = sql.find("\n", index + 2)
            index = length if end < 0 else end + 1
            continue
        if char == "#":
            end = sql.find("\n", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated SQL block comment")
            index = end + 2
            continue
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 2 if sql[index] == "\\" and index + 1 < length else 1
            else:
                raise ValueError("Unterminated SQL string literal")
            continue
        if char == "$":
            tag_match = re.match(r"\$[A-Za-z_](?a:\w)*\$|\$\$", sql[index:])
            if tag_match:
                delimiter = tag_match.group(0)
                end = sql.find(delimiter, index + len(delimiter))
                if end < 0:
                    raise ValueError("Unterminated SQL dollar-quoted literal")
                index = end + len(delimiter)
                continue
        if char in {'"', "`", "["}:
            close = "]" if char == "[" else char
            end = sql.find(close, index + 1)
            if end < 0:
                raise ValueError("Unterminated quoted SQL identifier")
            tokens.append(("identifier", sql[index + 1 : end].replace(close + close, close)))
            index = end + 1
            continue
        if char.isascii() and (char.isalpha() or char == "_"):
            end = index + 1
            while end < length and sql[end].isascii() and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(("identifier", sql[index:end]))
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < length and (sql[end].isdigit() or sql[end] == "."):
                end += 1
            tokens.append(("number", sql[index:end]))
            index = end
            continue
        tokens.append(("punctuation", char))
        index += 1
    return tokens


def _strip_sql_comments(sql: str) -> str:
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    """Remove comments without touching SQL literals, so an appended cap stays executable."""
    result: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if char == "-" and following == "-":
            end = sql.find("\n", index + 2)
            index = length if end < 0 else end
            continue
        if char == "#":
            end = sql.find("\n", index + 1)
            index = length if end < 0 else end
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated SQL block comment")
            index = end + 2
            continue
        result.append(char)
        if char == "'":
            index += 1
            while index < length:
                quoted = sql[index]
                result.append(quoted)
                if quoted == "\\" and index + 1 < length:
                    result.append(sql[index + 1])
                    index += 2
                    continue
                if quoted == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        result.append(sql[index + 1])
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        index += 1
    return "".join(result)


# SQL keywords/operators that may appear bare in expressions and must never be
# treated as column references. Lower-cased for membership checks.
_SQL_NONCOLUMN_TOKENS = frozenset(
    {
        "select",
        "distinct",
        "all",
        "from",
        "where",
        "group",
        "by",
        "having",
        "order",
        "asc",
        "desc",
        "limit",
        "offset",
        "fetch",
        "first",
        "next",
        "rows",
        "row",
        "only",
        "join",
        "inner",
        "left",
        "right",
        "full",
        "outer",
        "cross",
        "on",
        "using",
        "as",
        "and",
        "or",
        "not",
        "in",
        "is",
        "null",
        "like",
        "ilike",
        "between",
        "exists",
        "case",
        "when",
        "then",
        "else",
        "end",
        "cast",
        "true",
        "false",
        "unknown",
        "union",
        "intersect",
        "except",
        "with",
        "over",
        "partition",
        "filter",
        "within",
        "nulls",
        "collate",
        "escape",
        "interval",
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
    }
)


def _bare_identifier(token: str) -> str:
    """Return the unquoted final segment of a (possibly dotted/quoted) identifier."""
    last = token.rsplit(".", 1)[-1].strip()
    return last.strip('"`[] ')


def _template_sql(definition: dict[str, Any], *, require_predicate: bool = True) -> str:
    sql = str(definition.get("sql") or definition.get("sql_template") or "").strip()
    if not sql:
        raise ValueError("query_definition_jsonb.sql is required")
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    normalized_sql = _strip_sql_comments(sql).strip()
    tokens = _sql_tokens(normalized_sql)
    if tokens and tokens[-1] == ("punctuation", ";"):
        tokens.pop()
        normalized_sql = normalized_sql.rstrip().removesuffix(";").rstrip()
    if not tokens or any(token == ("punctuation", ";") for token in tokens):
        raise ValueError("Only single-statement read-only queries are allowed")
    if tokens[0][0] != "identifier" or tokens[0][1].lower() != "select":
        raise ValueError(_MESSAGE_ONLY_READ_ONLY_SELECT_STATEMENTS_ARE_ALLOWED)
    words = {value.lower() for kind, value in tokens if kind == "identifier"}
    if words & _READ_ONLY_FORBIDDEN_TOKENS:
        raise ValueError(_MESSAGE_ONLY_READ_ONLY_SELECT_STATEMENTS_ARE_ALLOWED)
    for index, (kind, value) in enumerate(tokens[:-1]):
        if kind != "identifier" or tokens[index + 1] != ("punctuation", "("):
            continue
        lowered = value.lower()
        # A keyword before "(" is syntax, not a call: JOIN ... USING (id), ON (a = b),
        # EXISTS (subquery), IN (...), JOIN (subquery). Only a name that calls something
        # must be a known read-only function.
        if lowered in _SQL_NONCOLUMN_TOKENS:
            continue
        if lowered not in _READ_ONLY_FUNCTIONS:
            raise ValueError(_MESSAGE_ONLY_READ_ONLY_SELECT_STATEMENTS_ARE_ALLOWED)
    if require_predicate and "where" not in words and "limit" not in words:
        raise ValueError("Query must include a WHERE clause or LIMIT")
    return normalized_sql


def _resource_key(value: str) -> str:
    return value.rsplit(".", 1)[-1]


def _qualified_identifier(tokens: list[tuple[str, str]], start: int) -> tuple[str, int]:
    if start >= len(tokens) or tokens[start][0] != "identifier":
        raise ValueError(_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES)
    parts = [tokens[start][1].strip()]
    index = start + 1
    while index + 1 < len(tokens) and tokens[index] == ("punctuation", "."):
        if tokens[index + 1][0] != "identifier":
            raise ValueError(_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES)
        parts.append(tokens[index + 1][1].strip())
        index += 2
    return ".".join(parts).lower(), index


def _referenced_resources(sql: str) -> set[str]:
    """Return every top-level table source; reject SQL shapes we cannot scope safely."""
    tokens = _sql_tokens(sql)
    resources: set[str] = set()
    in_source_clause = False
    expect_source = False
    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        lowered = value.lower() if kind == "identifier" else ""
        if kind == "identifier" and lowered in {"from", "join"}:
            in_source_clause = True
            expect_source = True
            index += 1
            continue
        if in_source_clause and kind == "identifier" and lowered in _SOURCE_CLAUSE_TERMINATORS:
            in_source_clause = False
            expect_source = False
            index += 1
            continue
        if expect_source:
            if value == "(":
                raise ValueError("Subqueries and derived tables are not allowed in dataset queries")
            resource, index = _qualified_identifier(tokens, index)
            resources.add(resource)
            expect_source = False
            continue
        if in_source_clause and value == ",":
            expect_source = True
        index += 1
    if expect_source or not resources:
        raise ValueError(_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES)
    return resources


def _source_bindings(sql: str) -> tuple[set[str], dict[str, str]]:
    """Return source resources and their table/alias names without guessing ownership."""
    tokens = _sql_tokens(sql)
    resources: set[str] = set()
    bindings: dict[str, str] = {}
    in_source_clause = False
    expect_source = False
    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        lowered = value.lower() if kind == "identifier" else ""
        if kind == "identifier" and lowered in {"from", "join"}:
            in_source_clause = True
            expect_source = True
            index += 1
            continue
        if in_source_clause and kind == "identifier" and lowered in _SOURCE_CLAUSE_TERMINATORS:
            in_source_clause = False
            expect_source = False
            index += 1
            continue
        if expect_source:
            if value == "(":
                raise ValueError("Subqueries and derived tables are not allowed in dataset queries")
            resource, index = _qualified_identifier(tokens, index)
            resources.add(resource)
            for binding in {resource, _resource_key(resource)}:
                existing = bindings.get(binding)
                if existing is not None and existing != resource:
                    raise ValueError(_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES)
                bindings[binding] = resource
            if index < len(tokens) and tokens[index] == ("identifier", "as"):
                index += 1
            if index < len(tokens) and tokens[index][0] == "identifier":
                alias = tokens[index][1].lower()
                if alias not in _SOURCE_CLAUSE_TERMINATORS and alias not in {
                    "join",
                    "inner",
                    "left",
                    "right",
                    "full",
                    "cross",
                    "outer",
                }:
                    existing = bindings.get(alias)
                    if existing is not None and existing != resource:
                        raise ValueError(
                            _MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES
                        )
                    bindings[alias] = resource
                    index += 1
            expect_source = False
            continue
        if in_source_clause and value == ",":
            expect_source = True
        index += 1
    if expect_source or not resources:
        raise ValueError(_MESSAGE_COULD_NOT_CONFIDENTLY_IDENTIFY_REFERENCED_RESOURCES)
    return resources, bindings


def _referenced_column_references(sql: str) -> list[tuple[str | None, str]]:
    """Return (owning resource, column) pairs; ambiguous columns stay unowned."""
    resources, bindings = _source_bindings(sql)
    stripped = _STRING_LITERAL.sub(" ", _PARAM_REFS.sub(" ", _strip_sql_comments(sql)))
    aliases = {name.lower() for name in re.findall(r"\bas\s+([A-Z_]\w*)", stripped, re.IGNORECASE)}
    references: list[tuple[str | None, str]] = []
    for match in _IDENTIFIER_REF.finditer(stripped):
        token = match.group(0)
        normalized = token.lower().replace(" ", "")
        if normalized in resources:
            continue
        if "." not in token and stripped[match.end() :].lstrip().startswith("("):
            continue
        parts = [_bare_identifier(part) for part in token.split(".")]
        column = parts[-1]
        lowered = column.lower()
        if not column or lowered in _SQL_NONCOLUMN_TOKENS:
            continue
        if len(parts) > 1:
            owner = bindings.get(parts[-2].lower())
            if owner is None:
                raise ValueError("Could not confidently identify column ownership")
            references.append((owner, column))
        elif lowered not in bindings and lowered not in aliases:
            references.append((None, column))
    return references


def _validate_query_scope(sql: str, allowed: dict[str, set[str] | None]) -> None:
    if not allowed:
        raise ValueError("Dataset resource scope is empty")
    referenced = _referenced_resources(sql)
    outside = sorted(resource for resource in referenced if resource not in allowed)
    if outside:
        raise ValueError(
            "Query references resources outside dataset scope: "
            f"{outside}. Allowed resources: {sorted(allowed)}"
        )
    if not any(allowed.get(resource) is not None for resource in referenced):
        return
    stripped = _STRING_LITERAL.sub(" ", _PARAM_REFS.sub(" ", _strip_sql_comments(sql)))
    projection = re.split(r"\bfrom\b", stripped, maxsplit=1, flags=re.IGNORECASE)[0]
    projection = re.sub(r"(?is)^\s*select\s+(?:distinct\s+)?", "", projection)
    if re.search(r"(?:^|,)\s*\*", projection):
        raise ValueError("Query references columns outside dataset scope: wildcard selection")
    outside_columns: list[str] = []
    for owner, column in _referenced_column_references(sql):
        resolved_owner = owner
        if resolved_owner is None:
            if len(referenced) != 1:
                raise ValueError("Could not confidently identify column ownership")
            resolved_owner = next(iter(referenced))
        permitted = allowed.get(resolved_owner)
        if permitted is not None and (column == "*" or column not in permitted):
            outside_columns.append(f"{resolved_owner}.{column}")
    if outside_columns:
        raise ValueError(
            f"Query references columns outside dataset scope: {sorted(set(outside_columns))}"
        )


def _limited_sql(prebuilt: str, sql: str, limit: int) -> str:
    """Apply a top-level, dialect-aware host row cap after lexical validation."""
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    normalized = _strip_sql_comments(sql).strip()
    tokens = _sql_tokens(normalized)
    depth = 0
    limit_positions: list[int] = []
    top_positions: list[int] = []
    for index, (kind, value) in enumerate(tokens):
        if value == "(":
            depth += 1
            continue
        if value == ")":
            depth -= 1
            continue
        if depth == 0 and kind == "identifier" and value.lower() == "limit":
            limit_positions.append(index)
        if depth == 0 and kind == "identifier" and value.lower() == "top":
            top_positions.append(index)
    normalized_prebuilt = _runtime_prebuilt({"prebuilt": prebuilt})
    if normalized_prebuilt in {"mssql", "cloud-sql-mssql"}:
        if limit_positions:
            raise ValueError("SQL Server queries must use TOP instead of LIMIT")
        if top_positions:
            raise ValueError(
                "SQL Server query already declares TOP; host row cap cannot be verified"
            )
        depth = 0
        for kind, value in tokens:
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
            elif (
                depth == 0
                and kind == "identifier"
                and value.lower() in {"union", "intersect", "except"}
            ):
                raise ValueError("SQL Server set queries are not supported by the bounded TOP form")
        match = re.match(r"(?is)^(\s*select\s+)(distinct\s+)?", normalized)
        if not match:
            raise ValueError(_MESSAGE_ONLY_READ_ONLY_SELECT_STATEMENTS_ARE_ALLOWED)
        return f"{match.group(1)}{match.group(2) or ''}TOP ({limit}) {normalized[match.end() :]}"
    if len(limit_positions) > 1:
        raise ValueError("Only one row limit is allowed")
    if limit_positions:
        position = limit_positions[0]
        if position + 1 >= len(tokens) or tokens[position + 1][0] != "number":
            raise ValueError("Query LIMIT must be a positive integer literal")
        if position + 2 < len(tokens) and tokens[position + 2] == ("punctuation", ","):
            raise ValueError("Query LIMIT offset syntax is not allowed")
        if (
            position + 2 < len(tokens)
            and tokens[position + 2][0] == "identifier"
            and tokens[position + 2][1].lower() == "by"
        ):
            raise ValueError("Query LIMIT BY is not a global row limit")
        raw_limit = tokens[position + 1][1]
        if not raw_limit.isdigit() or int(raw_limit) < 1:
            raise ValueError("Query LIMIT must be a positive integer literal")
        tail = tokens[position + 2 :]
        if tail and not (
            len(tail) == 2
            and tail[0][0] == "identifier"
            and tail[0][1].lower() == "offset"
            and tail[1][0] == "number"
            and tail[1][1].isdigit()
        ):
            raise ValueError(
                "Query LIMIT must be a single integer literal with optional integer OFFSET"
            )
        if int(raw_limit) > limit:
            raise ValueError(f"Query LIMIT exceeds the configured maximum of {limit}")
        return normalized
    return f"{normalized} LIMIT {limit}"


def _placeholder_occurrences(sql: str) -> list[tuple[int, int, str]]:
    if "\\" in sql or "$" in sql:
        raise ValueError(
            "Read SQL cannot contain backslashes or dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    occurrences: list[tuple[int, int, str]] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 2 if sql[index] == "\\" and index + 1 < length else 1
            continue
        if char in {'"', "`"}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "[":
            end = sql.find("]", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "-" and following == "-":
            end = sql.find("\n", index + 2)
            index = length if end < 0 else end + 1
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char == "$":
            tag_match = re.match(r"\$[A-Za-z_](?a:\w)*\$|\$\$", sql[index:])
            if tag_match:
                delimiter = tag_match.group(0)
                end = sql.find(delimiter, index + len(delimiter))
                index = length if end < 0 else end + len(delimiter)
                continue
        if char == ":":
            if following == ":":
                index += 2
                continue
            if following and (following.isalpha() or following == "_") and following.isascii():
                end = index + 2
                while end < length and (
                    sql[end].isascii() and (sql[end].isalnum() or sql[end] == "_")
                ):
                    end += 1
                occurrences.append((index, end, sql[index + 1 : end]))
                index = end
                continue
        index += 1
    return occurrences


def _placeholder_names(sql: str) -> list[str]:
    return [name for _start, _end, name in _placeholder_occurrences(sql)]


def _declared_parameter_types(schema: dict[str, Any]) -> dict[str, Any]:
    raw_properties = schema.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    return {
        str(name): declaration.get("type")
        for name, declaration in properties.items()
        if isinstance(declaration, dict)
    }


def _value_matches_declared_type(value: Any, declared: str) -> bool:
    if declared == "null":
        return value is None
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _validated_params(
    schema: dict[str, Any], placeholder_names: list[str], params: dict[str, Any]
) -> dict[str, Any]:
    declared_types = _declared_parameter_types(schema)
    referenced = set(placeholder_names)
    undeclared_placeholders = sorted(referenced - set(declared_types))
    if undeclared_placeholders:
        raise ValueError(
            f"Query parameter(s) must be declared in param_schema_jsonb: {undeclared_placeholders}"
        )
    missing = sorted(referenced - set(params))
    if missing:
        raise ValueError(f"Missing query parameter(s): {missing}")
    extra = sorted(set(params) - referenced)
    if extra:
        raise ValueError(f"Undeclared query parameter(s): {extra}")

    validated: dict[str, Any] = {}
    supported_types = {"string", "integer", "number", "boolean", "null"}
    for name in sorted(referenced):
        raw_declared = declared_types[name]
        declared_options = (
            [str(item) for item in raw_declared]
            if isinstance(raw_declared, list)
            else [str(raw_declared)]
        )
        if not declared_options or any(item not in supported_types for item in declared_options):
            raise ValueError(f"Query parameter '{name}' has unsupported declared type")
        value = params[name]
        if not any(_value_matches_declared_type(value, item) for item in declared_options):
            label = raw_declared if isinstance(raw_declared, str) else declared_options
            raise ValueError(f"Query parameter '{name}' must match declared type '{label}'")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Query parameter '{name}' must be finite")
        validated[name] = value
    return validated


def _params(request: dict[str, Any], sql: str) -> dict[str, Any]:
    raw_params = _action_input(request).get("params")
    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    else:
        raise ValueError("Query params must be an object")
    return _validated_params(_param_schema(request), _placeholder_names(sql), params)


def _validate_declared_params(request: dict[str, Any], sql: str) -> None:
    declared = set(_declared_parameter_types(_param_schema(request)))
    required = set(_placeholder_names(sql))
    missing = sorted(required - declared)
    if missing:
        raise ValueError(f"Query parameter(s) must be declared in param_schema_jsonb: {missing}")


def _compile_bound_query(
    prebuilt: str,
    sql: str,
    params: dict[str, Any],
    *,
    occurrences: list[tuple[int, int, str]] | None = None,
) -> dict[str, Any]:
    binding = _PARAMETER_BINDERS.get(prebuilt)
    if not binding:
        raise ValueError(f"No parameter binder is declared for Toolbox prebuilt '{prebuilt}'")
    if occurrences is None:
        occurrences = _placeholder_occurrences(sql)
    chunks: list[str] = []
    cursor = 0
    param_order: list[str] = []
    for position, (start, end, name) in enumerate(occurrences, start=1):
        if name not in params:
            raise ValueError(f"Unresolved query parameter: {name}")
        chunks.append(sql[cursor:start])
        if binding == "positional":
            chunks.append("?")
        elif binding in {"numbered", "numbered-map"}:
            chunks.append(f"${position}")
        elif binding == "named-positional":
            chunks.append(f"@p{position}")
        elif binding == "named":
            chunks.append(f"@{name}")
        else:
            raise ValueError(f"Unsupported parameter binder '{binding}'")
        param_order.append(name)
        cursor = end
    chunks.append(sql[cursor:])
    return {
        "sql": "".join(chunks),
        # Keep values in a string payload so Toolbox's generic MCP argument
        # decoding cannot turn a BIGINT into float64 before its driver sees it.
        "params_json": json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "param_order": param_order,
        "binding": binding,
    }


def _limit(request: dict[str, Any], *, key: str, default: int, maximum: int | None) -> int:
    """Resolve one request limit, clamping only where this extension owns the ceiling.

    ``maximum=None`` means the host already approved the value against its own
    deployment maximum, so re-clamping here would silently ignore an operator's
    configuration.
    """
    raw_limits = _as_dict(_action_input(request).get("limits"))
    try:
        value = int(raw_limits.get(key) or default)
    except (TypeError, ValueError):
        value = default
    if maximum is not None:
        value = min(value, maximum)
    return max(1, value)


def _output_schema_from_rows(rows: list[Any]) -> dict[str, Any]:
    properties: dict[str, dict[str, str]] = {}
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    for key, value in first.items():
        if isinstance(value, bool):
            value_type = "boolean"
        elif isinstance(value, int):
            value_type = "integer"
        elif isinstance(value, float):
            value_type = "number"
        elif value is None:
            value_type = "string"
        else:
            value_type = "string"
        properties[str(key)] = {"type": value_type}
    return {"type": "object", "properties": properties, "additionalProperties": True}


def _normalize_toolbox_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_toolbox_value(item) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) == 16 and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
            return str(uuid.UUID(bytes=bytes(value)))
        return [_normalize_toolbox_value(item) for item in value]
    return value


def _execute_sql(request: dict[str, Any], *, preview: bool) -> dict[str, Any]:
    config = _connection_config(request)
    prebuilt = _prebuilt(config)
    sql = _template_sql(_query_definition(request))
    _validate_query_scope(sql, _dataset_scope(request))
    params = _params(request, sql)
    limit = _limit(
        request,
        key="max_preview_rows" if preview else "max_runtime_rows",
        default=10 if preview else 100,
        # Preview owns its own ceiling; the runtime row limit is whatever the host
        # approved against FS_DATASET_QUERY_MAX_RUNTIME_ROWS.
        maximum=100 if preview else None,
    )
    limited_sql = _limited_sql(prebuilt, sql, limit)
    bound_query = _compile_bound_query(prebuilt, limited_sql, params)
    rows_raw = _invoke_toolbox(
        config,
        _secret_refs(request),
        "execute_sql",
        bound_query,
        timeout_seconds=_limit(request, key="timeout_seconds", default=30, maximum=120),
    )
    rows = [
        _normalize_toolbox_value(dict(row))
        for row in _toolbox_rows(rows_raw)
        if isinstance(row, dict)
    ]
    declared_schema = _as_dict(
        _as_dict(_action_input(request).get("query_template")).get("output_schema_jsonb")
    )
    output_schema = (
        deepcopy(declared_schema)
        if not rows and declared_schema
        else _output_schema_from_rows(rows)
    )
    return {
        "rows": rows[:limit],
        "row_count": min(len(rows), limit),
        "output_schema": output_schema,
        "truncated": len(rows) > limit,
    }


def _execute_write(request: dict[str, Any]) -> dict[str, Any]:
    try:
        raw, config, secrets, test_mode = write_input(request, TOOLBOX_DATASET_PREBUILTS)
        _validate_connection_credentials(config, secrets)
        sql, occurrences = write_statement(raw.get("sql"), prebuilt=str(config["prebuilt"]))
        params = _validated_params(
            raw.get("param_schema", {}), [name for _, _, name in occurrences], raw.get("params", {})
        )
        bound_query = _compile_bound_query(
            str(config["prebuilt"]), sql, params, occurrences=occurrences
        )
    except ValueError as exc:
        response = _envelope(ok=False, errors=[_error("invalid_payload", str(exc))])
        response.update(external_effect_status="failed", definitely_no_external_effect=True)
        return response
    if test_mode:
        response = _envelope(ok=True, result={"test_mode_status": "suppressed"})
        response.update(external_effect_status="suppressed", definitely_no_external_effect=True)
        return response
    try:
        raw_rows = _invoke_toolbox(config, secrets, "execute_sql", bound_query, timeout_seconds=30)
        rows = [_normalize_toolbox_value(row) for row in _toolbox_rows(raw_rows)]
    except Exception:
        # A server may have committed before the transport failed. Do not leak SQL,
        # values or credentials, and do not tell the host that retrying is safe.
        response = _envelope(
            ok=False,
            errors=[
                _error(
                    "write_outcome_unknown",
                    "Database write outcome is unknown; inspect the database before retrying",
                )
            ],
        )
        response.update(
            external_effect_status="timeout_unknown", definitely_no_external_effect=False
        )
        return response
    response = _envelope(
        ok=True,
        result={"status": "executed", "rows": rows, "returned_row_count": len(rows)},
    )
    response["external_effect_status"] = "succeeded"
    return response


def handle_action(request: dict[str, Any]) -> dict[str, Any]:
    action = _as_dict(request.get("action"))
    action_id = str(action.get("action_id") or "").strip()
    if action_id in VECTOR_ACTIONS:
        return handle_vector_action(request)
    if action_id == "execute_write":
        return _execute_write(request)
    if action_id not in {
        "test_connection",
        "introspect_schema",
        "validate_query",
        "preview_query",
        "execute_template",
        "infer_output_schema",
    }:
        return _envelope(
            ok=False,
            errors=[
                _error("unknown_action", f"Unknown action_id: {action_id}", path="action.action_id")
            ],
        )
    try:
        config = _connection_config(request)
        prebuilt = _prebuilt(config)
        if action_id == "test_connection":
            _invoke_toolbox(
                config,
                _secret_refs(request),
                "list_tables",
                timeout_seconds=_limit(request, key="timeout_seconds", default=30, maximum=120),
            )
            return _envelope(
                ok=True,
                result={
                    "status": "healthy",
                    "connector_id": _connector_type_id(prebuilt),
                    "metadata": {"toolbox_prebuilt": prebuilt},
                },
            )
        if action_id == "introspect_schema":
            snapshot = _introspect(request)
            return _envelope(
                ok=True,
                result={
                    "schema_snapshot": snapshot,
                    "resources": snapshot["resources"],
                    "resource_count": len(snapshot["resources"]),
                },
            )
        if action_id == "validate_query":
            sql = _template_sql(_query_definition(request))
            _validate_query_scope(sql, _dataset_scope(request))
            _validate_declared_params(request, sql)
            preview = _execute_sql(request, preview=True)
            return _envelope(
                ok=True,
                result={
                    "valid": True,
                    "read_only": True,
                    "referenced_resources": sorted(_referenced_resources(sql)),
                    "row_count": preview["row_count"],
                    "output_schema": preview["output_schema"],
                },
            )
        if action_id == "preview_query":
            return _envelope(ok=True, result=_execute_sql(request, preview=True))
        if action_id == "execute_template":
            return _envelope(ok=True, result=_execute_sql(request, preview=False))
        if action_id == "infer_output_schema":
            preview = _execute_sql(request, preview=True)
            return _envelope(ok=True, result={"output_schema": preview["output_schema"]})
    except Exception as exc:
        code = _exception_code(exc)
        return _envelope(ok=False, errors=[_error(code, _redact(str(exc)), path="action.input")])
    return _envelope(ok=False, errors=[_error("connector_runtime_failed", "Unhandled action")])


def handle_query(request: dict[str, Any]) -> dict[str, Any]:
    query = _as_dict(request.get("query"))
    query_id = str(query.get("query_id") or "").strip()
    if query_id == "runtime_health":
        return _envelope(
            ok=True,
            result={"status": "healthy", "extension_id": EXTENSION_ID},
        )
    if query_id == "supported_connectors":
        return _envelope(
            ok=True,
            result={
                "connectors": _supported_connector_rows(),
            },
        )
    return _envelope(
        ok=False,
        errors=[_error("unknown_query", f"Unknown query_id: {query_id}", path="query.query_id")],
    )


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Check the host envelope, then dispatch one query or action."""
    # The host envelope is checked before Toolbox is started or a database is
    # reached: another contract, a missing execution-scope key or two owners
    # is refused here.
    try:
        runtime_context_from_payload(request)
    except ValueError as exc:
        response = _envelope(ok=False, errors=[_error("invalid_host_envelope", str(exc))])
        response["definitely_no_external_effect"] = True
        return response
    mode = str(request.get("mode") or "").strip().lower()
    return handle_action(request) if mode == "action" else handle_query(request)


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        request = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        response = _envelope(
            ok=False,
            errors=[_error("invalid_json", f"Malformed JSON input: {exc.msg}")],
        )
        print(json.dumps(response, sort_keys=True))
        return 2
    if not isinstance(request, dict):
        response = _envelope(
            ok=False,
            errors=[_error("invalid_payload", "Input payload must be a JSON object")],
        )
        print(json.dumps(response, sort_keys=True))
        return 2
    response = handle_request(request)
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
