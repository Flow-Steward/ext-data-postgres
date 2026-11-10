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
    import ctypes
    import errno
    import os
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
    # Capture the PID in the parent. Under load a correctly cancelled child can
    # be killed before it reaches a child-written marker or the native wait.
    # Reaping still must be verified for that child within the same 50 ms budget.
    forked = []
    original_fork = os.fork

    def observed_fork():
        pid = original_fork()
        if pid > 0:
            forked.append(pid)
        return pid

    monkeypatch.setattr(os, "fork", observed_fork)

    def blocking_resolver(*args, **kwargs):
        # This wait stays inside libc despite signal delivery, unlike Python sleep.
        # It models getaddrinfo's native EINTR-retrying DNS wait without live DNS.
        class Timespec(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]

        libc = ctypes.CDLL(None)
        for name in (
            "pthread_mutex_init",
            "pthread_mutex_destroy",
            "pthread_mutex_lock",
            "pthread_mutex_unlock",
            "pthread_cond_init",
            "pthread_cond_destroy",
        ):
            function = getattr(libc, name)
            function.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p] if name.endswith("_init") else [ctypes.c_void_p]
            )
            function.restype = ctypes.c_int
        libc.pthread_cond_timedwait.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(Timespec),
        ]
        libc.pthread_cond_timedwait.restype = ctypes.c_int
        mutex, condition = (ctypes.c_long * 16)(), (ctypes.c_long * 16)()
        assert libc.pthread_mutex_init(mutex, None) == 0
        assert libc.pthread_cond_init(condition, None) == 0
        assert libc.pthread_mutex_lock(mutex) == 0
        until = time.time() + 0.6
        absolute = Timespec(int(until), int((until % 1) * 1_000_000_000))
        try:
            assert (
                libc.pthread_cond_timedwait(condition, mutex, ctypes.byref(absolute))
                == errno.ETIMEDOUT
            )
        finally:
            libc.pthread_mutex_unlock(mutex)
            libc.pthread_cond_destroy(condition)
            libc.pthread_mutex_destroy(mutex)
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
    assert len(forked) == 1
    child_pid = forked[0]
    assert child_pid != os.getpid()
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pid, os.WNOHANG)
    assert launches == []
    assert set(threading.enumerate()) == previous_threads
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == previous_timer


def test_resolver_output_budget_fails_before_native_launch(monkeypatch):
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
    monkeypatch.setattr(toolbox, "_binary", lambda: "/unused-native")
    monkeypatch.setattr(toolbox, "_endpoint", lambda _config: tuple("8.8.8.8" for _ in range(2000)))
    launches = []

    def launch(*args, **kwargs):
        launches.append(True)
        raise AssertionError("oversized resolver output must not launch Toolbox")

    monkeypatch.setattr(toolbox.subprocess, "Popen", launch)
    with pytest.raises(Failure) as failure:
        toolbox.invoke(
            config,
            secrets,
            {"type": "postgres-sql", "statement": "SELECT 1", "parameters": [], "readOnly": True},
            {},
            timeout_seconds=1,
        )
    assert failure.value.code == "network_blocked"
    assert failure.value.dispatched is False
    assert launches == []
