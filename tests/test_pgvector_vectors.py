import pytest
from vector_commands import validate, execute, tool
from vector_validation import Failure

CONFIG = {"schema": "fs_vectors", "request_timeout_seconds": 15}


def test_invalid_vectors_and_identifiers_rejected_before_io():
    for args in (
        {"collection": "bad;drop", "vector": [1]},
        {"collection": "docs", "vector": [float("nan")]},
        {"collection": "docs", "vector": [True]},
        {"collection": "docs", "vector": [0, 0]},
        {"collection": "docs", "vector": [1], "limit": True},
    ):
        with pytest.raises(Failure):
            validate("query", args, CONFIG)


def test_values_are_bound_not_sql_interpolated():
    command, values = tool("SELECT $1::text", ["x'; DROP SCHEMA public;--"])
    assert values == {"p1": "x'; DROP SCHEMA public;--"}
    assert "DROP" not in command["statement"]


def test_dimension_and_metric_mismatch_stop_before_write():
    calls = []

    def call(command, args):
        calls.append(command)
        return [[{"dimensions": 3, "metric": "cosine", "valid": True}]]

    with pytest.raises(Failure, match="dimension"):
        execute(
            "upsert_chunk",
            {"collection": "docs", "chunk_id": "c", "document_id": "d", "vector": [1, 2]},
            CONFIG,
            {},
            call,
        )
    assert len(calls) == 1
