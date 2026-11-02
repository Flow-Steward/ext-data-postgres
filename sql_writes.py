"""Validation for workflow SQL writes; connections are supplied by the host."""

from __future__ import annotations

from collections.abc import Collection
import re
from typing import Any

WRITE_PREBUILTS = frozenset(["postgres", "alloydb-omni", "alloydb-postgres", "cloud-sql-postgres"])
_INPUT_FIELDS = frozenset({"connection_ref", "sql", "params", "param_schema"})
_FORBIDDEN_WORDS = frozenset(
    {
        "alter",
        "begin",
        "call",
        "commit",
        "copy",
        "create",
        "drop",
        "dumpfile",
        "exec",
        "execute",
        "go",
        "grant",
        "load",
        "outfile",
        "revoke",
        "rollback",
        "truncate",
        "declare",
        "print",
        "waitfor",
        "throw",
        "raiserror",
        "dbcc",
        "backup",
        "restore",
        "kill",
        "shutdown",
        "checkpoint",
        "reconfigure",
        "save",
        "lock",
        "unlock",
        "analyze",
        "vacuum",
        "explain",
        "replace",
        "merge",
        "if",
        "while",
        "return",
        "use",
    }
)


def write_input(
    request: dict[str, Any], offered: Collection[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    """Return validated input, host connection config/secrets and test-mode flag."""
    action = request.get("action")
    raw = action.get("input") if isinstance(action, dict) else None
    if not isinstance(raw, dict) or set(raw) - _INPUT_FIELDS:
        raise ValueError(
            "Write input must contain only connection_ref, sql, params and param_schema"
        )
    reference = raw.get("connection_ref")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("An explicit connection_ref is required")
    target = action.get("target") if isinstance(action, dict) else None
    connection = target.get("connection") if isinstance(target, dict) else None
    if not isinstance(connection, dict) or connection.get("connection_id") != reference:
        raise ValueError("The host connection must match connection_ref")
    connection_type = connection.get("connection_type") or connection.get("connection_type_id")
    if connection_type != "toolbox_sql_writer":
        raise ValueError("A Toolbox SQL writer connection is required")
    config = connection.get("config")
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("prebuilt"), str)
        or config["prebuilt"] not in WRITE_PREBUILTS & set(offered)
    ):
        raise ValueError("This database does not offer workflow writes")
    secrets = connection.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError("Invalid host connection secrets")
    if not isinstance(raw.get("params", {}), dict):
        raise ValueError("Write params must be an object")
    if not isinstance(raw.get("param_schema", {}), dict):
        raise ValueError("Write param_schema must be an object")
    context = request.get("runtime_context")
    test_mode = context.get("test_mode", False) if isinstance(context, dict) else False
    if not isinstance(test_mode, bool):
        raise ValueError("runtime_context.test_mode must be boolean")
    return raw, dict(config), {"values": dict(secrets)}, test_mode


def _write_tokens(
    sql: str, *, mysql: bool, postgres: bool
) -> tuple[list[tuple[str, int]], list[tuple[int, int, str]]]:
    """Scan code and placeholders without modifying literals or dialect comments."""
    if "$" in sql:
        raise ValueError(
            "Write SQL cannot contain dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    tokens: list[tuple[str, int]] = []
    placeholders: list[tuple[int, int, str]] = []
    index, depth = 0, 0
    while index < len(sql):
        char = sql[index]
        pair = sql[index : index + 2]
        if char.isspace():
            index += 1
            continue
        if (pair == "--" and (not mysql or index + 2 == len(sql) or ord(sql[index + 2]) <= 32)) or (
            mysql and char == "#"
        ):
            end = sql.find("\n", index)
            index = len(sql) if end < 0 else end + 1
            continue
        if pair == "/*":
            if mysql and (
                sql[index : index + 3] == "/*!" or sql[index : index + 4].lower() == "/*m!"
            ):
                raise ValueError("Executable SQL comments are not supported")
            nesting = 1
            index += 2
            while index < len(sql) and nesting:
                if sql[index : index + 2] == "/*" and not mysql:
                    nesting += 1
                    index += 2
                elif sql[index : index + 2] == "*/":
                    nesting -= 1
                    index += 2
                else:
                    index += 1
            if nesting:
                raise ValueError("Unterminated SQL comment")
            continue
        if char in {"'", '"', "`", "["}:
            close = "]" if char == "[" else char
            index += 1
            while index < len(sql):
                if sql[index] == "\\" and (mysql or char == "'"):
                    raise ValueError("Use bound parameters for backslash-containing SQL literals")
                if sql[index] == close:
                    if index + 1 < len(sql) and sql[index + 1] == close:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("Unterminated SQL quoted value")
            tokens.append(("<quoted>", depth))
            continue
        if char == "$" and postgres:
            match = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", sql[index:])
            if match:
                delimiter = match.group(0)
                end = sql.find(delimiter, index + len(delimiter))
                if end < 0:
                    raise ValueError("Unterminated SQL dollar literal")
                index = end + len(delimiter)
                tokens.append(("<quoted>", depth))
                continue
        if pair == "::":
            index += 2
            continue
        if char == ":":
            match = re.match(r":[A-Za-z_][A-Za-z_0-9]*", sql[index:])
            if match:
                end = index + len(match.group(0))
                placeholders.append((index, end, sql[index + 1 : end]))
                tokens.append(("<parameter>", depth))
                index = end
                continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append((sql[index:end].lower(), depth))
            index = end
            continue
        if char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced SQL parentheses")
        tokens.append((char, depth))
        if char == "(":
            depth += 1
        index += 1
    if depth:
        raise ValueError("Unbalanced SQL parentheses")
    return tokens, placeholders


def write_statement(value: Any, *, prebuilt: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Validate one DML statement and return unchanged SQL with placeholder spans."""
    if not isinstance(value, str) or not value.strip() or len(value) > 65536:
        raise ValueError("sql must be nonempty text of at most 65536 characters")
    if "$" in value:
        raise ValueError(
            "Write SQL cannot contain dollar-quote syntax (raw dollar characters); use bound parameters"
        )
    sql = value.strip()
    tokens, placeholders = _write_tokens(
        sql,
        mysql=prebuilt in {"mysql", "mariadb", "cloud-sql-mysql"},
        postgres=prebuilt in {"postgres", "alloydb-omni", "alloydb-postgres", "cloud-sql-postgres"},
    )
    if tokens and tokens[-1][0] == ";":
        tokens.pop()
    if not tokens or any(word == ";" for word, _ in tokens):
        raise ValueError("Exactly one SQL write statement is required")
    first = tokens[0][0]
    if first not in {"insert", "update", "delete"}:
        raise ValueError("Only INSERT, UPDATE and DELETE statements are supported")
    if {word for word, _ in tokens} & _FORBIDDEN_WORDS:
        raise ValueError("DDL, transaction control and executable SQL scripts are not supported")
    top = [word for word, depth in tokens if depth == 0]
    upsert_seen = False
    for position, word in enumerate(top[1:], start=1):
        if word not in {"insert", "update", "delete"}:
            continue
        conflict = top[position - 1] == "do" and "conflict" in top[:position]
        duplicate = top[max(0, position - 3) : position] == ["on", "duplicate", "key"]
        if first == "insert" and word == "update" and not upsert_seen and (conflict or duplicate):
            upsert_seen = True
            continue
        raise ValueError("Multiple SQL write statements are not supported")
    if top.count("set") > 1 or ("select" in top and (first != "insert" or top.count("select") > 1)):
        raise ValueError("SQL batches are not supported")
    return sql, placeholders
