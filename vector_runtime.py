"""Vector workflow actions on the existing PostgreSQL project connection."""

from __future__ import annotations
import json
import time
from flowsteward_extension_sdk import runtime_context_from_payload
import vector_toolbox as toolbox
from vector_commands import WRITES, execute, validate
from vector_validation import Failure, bounded_json, connection_values, require


def handle(payload):
    write = False
    dispatched = False
    try:
        bounded_json(payload)
        runtime_context_from_payload(payload)
        require(payload.get("mode") == "action", "An action invocation is required.")
        action = payload.get("action") or {}
        name = action.get("action_id")
        args = action.get("input", {})
        require(isinstance(args, dict), "Input must be an object.")
        bounded_json(args)
        context = payload["runtime_context"]
        for scope_id in ("account_id", "project_id"):
            value = context.get(scope_id)
            require(
                isinstance(value, str) and bool(value.strip()),
                "A project-scoped account and project are required.",
            )
        require(type(context.get("test_mode", False)) is bool, "test_mode must be boolean.")
        connection = (action.get("target") or {}).get("connection") or {}
        require(
            isinstance(connection, dict)
            and isinstance(args.get("connection_ref"), str)
            and bool(args["connection_ref"].strip())
            and connection.get("connection_id") == args["connection_ref"]
            and (connection.get("connection_type") or connection.get("connection_type_id"))
            == "toolbox_sql_writer",
            "Select the matching PostgreSQL project connection.",
            code="invalid_connection",
        )
        config, secrets = connection_values(connection)
        validate(name, args, config)
        write = name in WRITES
        if write:
            if context.get("test_mode"):
                raise Failure(
                    "test_mode_suppressed", "pgvector writes are suppressed in test mode."
                )
            claim = context.get("external_effect_id")
            require(
                isinstance(claim, str) and bool(claim.strip()) and len(claim) <= 512,
                "A durable external-effect claim is required.",
                code="effect_claim_required",
            )
        deadline = time.monotonic() + config["request_timeout_seconds"]

        def call(command, arguments):
            nonlocal dispatched
            remaining = deadline - time.monotonic()
            require(remaining > 0, "Operation deadline exceeded.", code="timeout")
            try:
                value = toolbox.invoke(
                    config, secrets, command, arguments, timeout_seconds=remaining
                )
                dispatched = dispatched or not command["readOnly"]
                return value
            except Failure as exc:
                dispatched = dispatched or (exc.dispatched and not command["readOnly"])
                raise
            except Exception:
                dispatched = dispatched or not command["readOnly"]
                raise

        result = execute(name, args, config, context, call)
        require(
            len(json.dumps(result, allow_nan=False).encode()) <= 480 * 1024,
            "Provider result exceeds host inline budget.",
            code="response_too_large",
        )
        return {"ok": True, "result": result, "errors": []}
    except (Failure, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        code = exc.code if isinstance(exc, Failure) else "invalid_payload"
        message = (
            str(exc)
            if isinstance(exc, Failure)
            else "Invalid bounded host envelope or action input."
        )
        uncertain = write and dispatched
        return {
            "ok": False,
            "error_code": code,
            "error": message,
            "errors": [{"code": code, "message": message}],
            "retryable": False,
            "definitely_no_external_effect": not uncertain,
            "external_effect_status": "timeout_unknown" if uncertain else "failed",
        }
    except Exception:
        return {
            "ok": False,
            "error_code": "provider_unavailable",
            "error": "pgvector operation failed.",
            "errors": [{"code": "provider_unavailable", "message": "pgvector operation failed."}],
            "retryable": False,
            "definitely_no_external_effect": not (write and dispatched),
            "external_effect_status": "timeout_unknown" if write and dispatched else "failed",
        }
