"""Closed, bounded pgvector inputs and connection values."""

import ipaddress
import json
import math
import re


class Failure(Exception):
    def __init__(self, code, message, *, dispatched=False):
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched


def require(condition, message, *, code="invalid_payload"):
    if not condition:
        raise Failure(code, message)


def text(value, name, maximum=65536, *, nonempty=False):
    require(isinstance(value, str), f"{name} must be text.")
    require(len(value.encode("utf-8")) <= maximum and "\x00" not in value, f"Invalid {name}.")
    require(not nonempty or bool(value.strip()), f"{name} must be nonempty.")
    return value


def identifier(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value),
        "Invalid SQL identifier.",
    )
    return value


def integer(value, name, low, high):
    require(
        type(value) is int and low <= value <= high,
        f"{name} must be an integer from {low} to {high}.",
    )
    return value


def boolean(value, name):
    require(type(value) is bool, f"{name} must be boolean.")
    return value


def bounded_json(value, limit=256 * 1024):
    try:
        require(
            len(json.dumps(value, allow_nan=False).encode()) <= limit, "JSON exceeds byte limit."
        )
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise Failure("invalid_payload", "Bounded valid JSON is required.") from exc


def vector(value):
    require(
        isinstance(value, list) and 1 <= len(value) <= 4096, "Vector requires 1 to 4096 values."
    )
    require(
        all(
            (type(v) is int and abs(v) <= 9007199254740991)
            or (type(v) is float and math.isfinite(v))
            for v in value
        ),
        "Vector values must be finite safe numbers.",
    )
    return [float(v) for v in value]


def connection_values(connection):
    """Normalize a SQL writer connection into closed vector transport settings."""
    try:
        config, secrets = connection.get("config") or {}, connection.get("secrets") or {}
        require(
            isinstance(config, dict) and isinstance(secrets, dict), "Invalid connection objects."
        )
        require(
            config.get("prebuilt") == "postgres", "Vectors require the direct PostgreSQL profile."
        )
        require(
            not config.get("env") and not config.get("query_params"),
            "Vector transport uses TLS and timeout settings, not environment/query overrides.",
        )
        host = text(config.get("host"), "host", 253, nonempty=True)
        require(
            not any(c.isspace() or c in "/\\@?#[]$%" for c in host),
            "Use a host without URL components.",
        )
        if ":" in host:
            ipaddress.IPv6Address(host)
        database = text(config.get("database"), "database", 63, nonempty=True)
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", database), "Invalid database.")
        port = config.get("port", 5432)
        if isinstance(port, str) and port.isascii() and port.isdigit():
            port = int(port)
        result = {
            "host": host,
            "port": integer(port, "port", 1, 65535),
            "tls": boolean(config.get("tls", True), "tls"),
            "database": database,
            "schema": identifier(config.get("schema", "fs_vectors")),
            "request_timeout_seconds": integer(
                config.get("request_timeout_seconds", 15), "request_timeout_seconds", 1, 30
            ),
        }
        secret_env = secrets.get("env") or {}
        require(isinstance(secret_env, dict), "Invalid secret environment.")
        credentials = {
            "username": text(config.get("user"), "user", 8192, nonempty=True),
            "password": text(
                secret_env.get("POSTGRES_PASSWORD", secrets.get("password")),
                "password",
                8192,
                nonempty=True,
            ),
        }
        return result, credentials
    except (Failure, ValueError, TypeError, UnicodeError) as exc:
        raise Failure(
            "invalid_connection", "Invalid PostgreSQL vector connection or credentials."
        ) from exc
