"""Bounded managed Toolbox subprocess; no shell or caller executable overrides."""

from __future__ import annotations
import contextlib
import hashlib
import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess  # nosec B404
import tempfile
import time
from flowsteward_extension_sdk import resolve_pinned_ips
from vector_validation import Failure, require

ROOT = Path(__file__).resolve().parent
MAX_OUTPUT = 1024 * 1024


def _binary():
    arch = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }.get(platform.machine().lower())
    if platform.system() != "Linux" or not arch:
        raise Failure("native_unavailable", "Toolbox requires Linux AMD64 or ARM64.")
    target = "linux-" + arch
    path = ROOT / "bin" / target / "toolbox"
    try:
        expected = json.loads((ROOT / "toolbox-provenance.json").read_text())["binaries"][target][
            "sha256"
        ]
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(
            actual == expected,
            "Packaged Toolbox checksum does not match.",
            code="native_unavailable",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Failure(
            "native_unavailable",
            "Install the packaged Toolbox binary for this architecture.",
        ) from exc
    return str(path)


def _allow_private():
    return os.environ.get("FS_ALLOW_PRIVATE_REMOTE_URLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _endpoint(config):
    try:
        ips = tuple(
            resolve_pinned_ips(config["host"], port=config["port"], purpose="pgvector endpoint")
        )
        require(bool(ips), "No endpoint addresses.", code="network_blocked")
        private = any(not ipaddress.ip_address(ip).is_global for ip in ips)
        require(
            not private or _allow_private(),
            "Private endpoints require host administrator permission.",
            code="network_blocked",
        )
        require(
            config["tls"] or all(not ipaddress.ip_address(ip).is_global for ip in ips),
            "Public endpoints require TLS.",
            code="network_blocked",
        )
        return ips
    except (ValueError, OSError) as exc:
        raise Failure("network_blocked", "Endpoint blocked by host network policy.") from exc


_MAX_RESOLVER_OUTPUT = 8192


def _resolver_child(config, write_fd, read_fd):
    """Resolve with inherited SDK policy; emit only a bounded private pipe result."""
    try:
        os.close(read_fd)
        null_fd = os.open(os.devnull, os.O_RDWR)
        for descriptor in (0, 1, 2):
            os.dup2(null_fd, descriptor)
        if null_fd > 2:
            os.close(null_fd)
        try:
            result = {"ips": list(_endpoint(config))}
        except Exception:
            result = {"error": True}
        encoded = json.dumps(result, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_RESOLVER_OUTPUT:
            encoded = b'{"error":true}'
        while encoded:
            written = os.write(write_fd, encoded)
            if written <= 0:
                break
            encoded = encoded[written:]
    finally:
        # Do not run inherited application exit handlers or flush host stdout.
        os._exit(0)


def _reap_resolver(process):
    if process.is_alive():
        process.kill()
    process.join()
    process.close()


def _resolve_endpoint(config, deadline):
    """Enforce the deadline independently of libc's blocking DNS implementation."""
    require(deadline > time.monotonic(), "Endpoint resolution deadline exceeded.", code="timeout")
    read_fd, write_fd = os.pipe()
    process = multiprocessing.get_context("fork").Process(
        target=_resolver_child, args=(config, write_fd, read_fd), daemon=True
    )
    try:
        process.start()
    except (OSError, RuntimeError) as exc:
        os.close(read_fd)
        os.close(write_fd)
        process.close()
        raise Failure("network_blocked", "Endpoint resolver is unavailable.") from exc
    os.close(write_fd)
    output = bytearray()
    try:
        os.set_blocking(read_fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise Failure("timeout", "Endpoint resolution deadline exceeded.")
                try:
                    chunk = os.read(read_fd, min(4096, _MAX_RESOLVER_OUTPUT + 1 - len(output)))
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                output.extend(chunk)
                require(
                    len(output) <= _MAX_RESOLVER_OUTPUT,
                    "Endpoint resolver output exceeded its byte limit.",
                    code="network_blocked",
                )
        try:
            result = json.loads(output)
            require(
                isinstance(result, dict)
                and set(result) == {"ips"}
                and isinstance(result["ips"], list)
                and bool(result["ips"])
                and all(isinstance(ip, str) for ip in result["ips"]),
                "Endpoint resolution failed or exceeded its output budget.",
                code="network_blocked",
            )
            ips = tuple(result["ips"])
            for ip in ips:
                ipaddress.ip_address(ip)
            return ips
        except (ValueError, TypeError, UnicodeError) as exc:
            raise Failure(
                "network_blocked", "Endpoint resolver returned an invalid result."
            ) from exc
    finally:
        os.close(read_fd)
        _reap_resolver(process)


def managed_config(config, secrets, command, arguments, ip=None):
    source = {
        "kind": "source",
        "name": "pg-source",
        "type": "postgres",
        "host": config["host"],
        "port": str(config["port"]),
        "database": config["database"],
        "user": secrets["username"],
        "password": secrets["password"],
        "queryParams": {
            "sslmode": "verify-full" if config["tls"] else "disable",
            "connect_timeout": "5",
            "statement_timeout": str(config["request_timeout_seconds"] * 1000),
            "lock_timeout": "3000",
            "search_path": "pg_catalog,public",
            "fs_pinned_ips": ",".join(ip or ()),
            "fs_vector_mode": "true",
            "fs_allow_private": str(_allow_private()).lower(),
        },
    }
    command = {k: v for k, v in command.items() if k != "readOnly"}
    tool = {
        "kind": "tool",
        "name": "pg-command",
        "source": "pg-source",
        "description": "One bounded fixed dense-vector operation.",
        **command,
    }
    return (
        "\n---\n".join(
            json.dumps(d, ensure_ascii=True).replace("$", "\\u0024") for d in (source, tool)
        )
        + "\n"
    )


def _send(process, payload, deadline):
    if process.stdin is None:
        raise Failure("provider_unavailable", "Toolbox input is unavailable.")
    data = memoryview(json.dumps(payload, allow_nan=False, separators=(",", ":")).encode() + b"\n")
    require(len(data) <= 264 * 1024, "Toolbox input exceeds its byte limit.")
    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise Failure("timeout", "Toolbox input deadline exceeded.")
            try:
                written = os.write(fd, data[:65536])
            except BlockingIOError:
                continue
            if written <= 0:
                raise Failure("provider_unavailable", "Toolbox input closed.")
            data = data[written:]


def _read(process, request_id, deadline, state):
    require(
        process.stdout is not None and process.stderr is not None,
        "Toolbox streams unavailable.",
        code="provider_unavailable",
    )
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure("timeout", "Toolbox deadline exceeded.")
            events = selector.select(remaining)
            if not events:
                raise Failure("timeout", "Toolbox deadline exceeded.")
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                state["bytes"] += len(chunk)
                require(
                    state["bytes"] <= MAX_OUTPUT,
                    "Toolbox output exceeded its byte limit.",
                    code="response_too_large",
                )
                if key.data == "stderr":
                    state["stderr"] = (state.get("stderr", b"") + chunk)[-8192:]
                    continue
                state["buffer"].extend(chunk)
                while b"\n" in state["buffer"]:
                    line, _, tail = state["buffer"].partition(b"\n")
                    state["buffer"] = bytearray(tail)
                    try:
                        data = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(data, dict) and data.get("id") == request_id:
                        require(
                            data.get("jsonrpc") == "2.0",
                            "Invalid Toolbox protocol.",
                            code="invalid_provider_response",
                        )
                        return data
    detail = state.get("stderr", b"").decode("utf-8", errors="replace").lower()
    code = (
        "authentication_failed"
        if any(word in detail for word in ("authentication", "unauthorized"))
        else "provider_unavailable"
    )
    raise Failure(code, "Toolbox closed before its response.")


def _stop(process):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _result(response):
    if response.get("error"):
        raise Failure("provider_unavailable", "Toolbox rejected the command.")
    result = response.get("result")
    require(
        isinstance(result, dict),
        "Invalid Toolbox result.",
        code="invalid_provider_response",
    )
    if result.get("isError"):
        detail = str(result.get("content", ""))[:8192].lower()
        if "exceeds result byte limit" in detail or "exceeds result row limit" in detail:
            code = "response_too_large"
        elif (
            "authentication" in detail
            or "unauthorized" in detail
            or "security.unauthorized" in detail
        ):
            code = "authentication_failed"
        else:
            code = "provider_unavailable"
        raise Failure(code, "pgvector command failed.")
    content = result.get("content")
    require(
        isinstance(content, list),
        "Invalid Toolbox content.",
        code="invalid_provider_response",
    )
    values = []
    for block in content:
        require(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str),
            "Invalid Toolbox content.",
            code="invalid_provider_response",
        )
        try:
            values.append(json.loads(block["text"]))
        except (ValueError, TypeError) as exc:
            raise Failure("invalid_provider_response", "Toolbox result is not JSON.") from exc
    return values


def invoke(config, secrets, command, arguments, *, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    executable = _binary()
    ip = _resolve_endpoint(config, deadline)
    dispatched = False
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="fs-pgvector-") as directory:
            path = Path(directory) / "tools.yaml"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(managed_config(config, secrets, command, arguments, ip))
            require(time.monotonic() < deadline, "Toolbox deadline exceeded.", code="timeout")
            process = subprocess.Popen(  # nosec B603
                [
                    executable,
                    "--config",
                    str(path),
                    "--stdio",
                    "--disable-version-check",
                    "--disable-reload",
                    "--log-level",
                    "ERROR",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LANG": "C.UTF-8"},
                start_new_session=True,
            )
            state = {"bytes": 0, "buffer": bytearray()}
            try:
                _send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "flowsteward.data-connector-mcp-toolbox-postgres",
                                "version": "1.4.3",
                            },
                        },
                    },
                    deadline,
                )
                initial = _read(process, 1, deadline, state)
                require(
                    not initial.get("error") and isinstance(initial.get("result"), dict),
                    "Toolbox initialization failed.",
                    code="provider_unavailable",
                )
                _send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    deadline,
                )
                dispatched = True
                _send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "pg-command", "arguments": arguments},
                    },
                    deadline,
                )
                return _result(_read(process, 2, deadline, state))
            finally:
                _stop(process)
    except Failure as exc:
        exc.dispatched = exc.dispatched or dispatched
        raise
    except Exception as exc:
        raise Failure(
            "provider_unavailable", "Toolbox process failed.", dispatched=dispatched
        ) from exc
