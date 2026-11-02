import json
from unittest.mock import patch
import pytest
import vector_toolbox as toolbox
from vector_validation import Failure, connection_values


def test_policy_pin_and_public_tls(monkeypatch):
    config = {"host": "db.example", "port": 5432, "tls": False}
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    with patch.object(toolbox, "resolve_pinned_ips", return_value=["127.0.0.1"]):
        with pytest.raises(Failure):
            toolbox._endpoint(config)
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    with patch.object(toolbox, "resolve_pinned_ips", return_value=["8.8.8.8"]):
        with pytest.raises(Failure):
            toolbox._endpoint(config)
        assert toolbox._endpoint({**config, "tls": True}) == ("8.8.8.8",)


def test_managed_config_pins_address_preserves_tls_hostname_and_no_env_expansion():
    config, secrets = connection_values(
        {
            "config": {
                "prebuilt": "postgres",
                "host": "db.example",
                "database": "vectors",
                "user": "user",
            },
            "secrets": {"password": "$secret"},
        }
    )
    raw = toolbox.managed_config(
        config,
        secrets,
        {"type": "postgres-sql", "statement": "SELECT $1", "parameters": [], "readOnly": True},
        {},
        ("8.8.8.8",),
    )
    source, command = [json.loads(d) for d in raw.split("\n---\n")]
    assert "$" not in raw and source["password"] == "$secret"
    assert source["host"] == "db.example" and source["queryParams"]["sslmode"] == "verify-full"
    assert source["queryParams"]["fs_pinned_ips"] == "8.8.8.8"
    assert source["queryParams"]["fs_vector_mode"] == "true"
    assert command["statement"] == "SELECT $1" and "readOnly" not in command


def test_blocking_endpoint_resolution_respects_remaining_deadline(monkeypatch):
    """A stalled synchronous resolver cannot outlive the action or launch Toolbox."""
    import signal
    import socket
    import threading
    import time

    config, secrets = connection_values(
        {
            "config": {
                "prebuilt": "postgres",
                "host": "db.example",
                "database": "vectors",
                "user": "u",
            },
            "secrets": {"password": "fixture-password"},
        }
    )
    launches = []

    def blocking_resolver(*args, **kwargs):
        time.sleep(0.6)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 5432))]

    def launch(*args, **kwargs):
        launches.append(True)
        raise AssertionError("expired resolution must never launch a process")

    monkeypatch.setattr(socket, "getaddrinfo", blocking_resolver)
    monkeypatch.setattr(toolbox, "_binary", lambda: "/unused-native")
    monkeypatch.setattr(toolbox.subprocess, "Popen", launch)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    previous_threads = set(threading.enumerate())
    started = time.monotonic()
    with pytest.raises(Failure) as failure:
        toolbox.invoke(
            config,
            secrets,
            {"type": "postgres-sql", "statement": "SELECT 1", "parameters": [], "readOnly": True},
            {},
            timeout_seconds=0.05,
        )
    elapsed = time.monotonic() - started
    assert failure.value.code == "timeout"
    assert failure.value.dispatched is False
    assert elapsed < 0.4
    assert launches == []
    assert set(threading.enumerate()) == previous_threads
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == previous_timer
