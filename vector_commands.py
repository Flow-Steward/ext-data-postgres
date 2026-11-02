"""Closed dense-vector operations compiled to fixed PostgreSQL statements."""

import json
import math
from vector_validation import require, identifier, integer, text, vector, bounded_json

WRITES = {
    "ensure_collection",
    "drop_collection",
    "upsert_chunk",
    "upsert_document",
    "update_chunk",
    "delete_chunk",
    "delete_document",
}
FIELDS = {
    "ensure_collection": {"collection", "dimensions", "metric"},
    "get_collection": {"collection"},
    "drop_collection": {"collection", "confirm"},
    "upsert_chunk": {
        "collection",
        "chunk_id",
        "document_id",
        "vector",
        "content",
        "metadata",
        "source",
    },
    "upsert_document": {"collection", "document_id", "chunks", "source"},
    "get_chunk": {"collection", "chunk_id", "include_vector"},
    "update_chunk": {"collection", "chunk_id", "vector", "content", "metadata", "source"},
    "delete_chunk": {"collection", "chunk_id"},
    "delete_document": {"collection", "document_id"},
    "query": {"collection", "vector", "limit", "min_score", "metadata_filter", "include_vector"},
}
OPTIONAL = {
    "metric",
    "content",
    "metadata",
    "source",
    "limit",
    "min_score",
    "metadata_filter",
    "include_vector",
}
METRICS = {
    "cosine": ("vector_cosine_ops", "<=>"),
    "l2": ("vector_l2_ops", "<->"),
    "inner_product": ("vector_ip_ops", "<#>"),
}


def validate(name, args, config):
    require(name in FIELDS and isinstance(args, dict), "Unknown pgvector action.")
    require(set(args) <= FIELDS[name] | {"connection_ref"}, "Unknown action input.")
    required = FIELDS[name] - OPTIONAL
    if name == "update_chunk":
        required = {"collection", "chunk_id"}
    require(required <= set(args), "Missing required action input.")
    a = dict(args)
    identifier(a.get("collection"))
    require(len(a["collection"]) <= 48, "Collection identifier exceeds 48 characters.")
    for key in ("chunk_id", "document_id"):
        if key in a:
            text(a[key], key, 256, nonempty=True)
    if "vector" in a:
        a["vector"] = vector(a["vector"])
        require(
            len(a["vector"]) <= 2000 and any(a["vector"]),
            "Vector requires 1 to 2000 finite values and nonzero norm.",
            code="invalid_vector",
        )
    for key in ("metadata", "source", "metadata_filter"):
        if key in a:
            require(isinstance(a[key], dict), key + " must be an object.")
            bounded_json(a[key], 32768)
    if "content" in a:
        text(a["content"], "content", 65536)
    if "include_vector" in a:
        require(type(a["include_vector"]) is bool, "include_vector must be boolean.")
    if name == "ensure_collection":
        integer(a["dimensions"], "dimensions", 1, 2000)
        a["metric"] = a.get("metric", "cosine")
        require(a["metric"] in METRICS, "Invalid vector metric.")
    if name == "drop_collection":
        require(a["confirm"] is True, "Dropping a collection requires confirm=true.")
    if name == "query":
        a["limit"] = integer(a.get("limit", 20), "limit", 1, 100)
        if "min_score" in a:
            require(
                type(a["min_score"]) in (int, float) and math.isfinite(a["min_score"]),
                "min_score must be finite.",
            )
    if name == "update_chunk":
        require(
            bool(set(a) & {"vector", "content", "metadata", "source"}),
            "An update field is required.",
        )
    if name == "upsert_document":
        require(
            isinstance(a["chunks"], list) and 1 <= len(a["chunks"]) <= 100,
            "Provide 1 to 100 chunks.",
        )
        seen = set()
        for chunk in a["chunks"]:
            require(
                isinstance(chunk, dict)
                and not (set(chunk) - {"chunk_id", "vector", "content", "metadata"}),
                "Invalid document chunk.",
            )
            validate(
                "upsert_chunk",
                {"collection": a["collection"], "document_id": a["document_id"], **chunk},
                config,
            )
            require(chunk["chunk_id"] not in seen, "Duplicate chunk ID.")
            seen.add(chunk["chunk_id"])
    return a


# Only identifiers validated with a closed ASCII grammar are interpolated into SQL.
# All content, IDs, JSON, vectors, limits, and filters are driver-bound parameters.
def q(value):
    return '"' + identifier(value) + '"'


def tool(statement, params=(), write=False):
    values = {"p" + str(i + 1): str(v) for i, v in enumerate(params)}
    return {
        "type": "postgres-sql",
        "statement": statement,
        "parameters": [
            {"name": k, "type": "string", "description": "Bound operation value."} for k in values
        ],
        "annotations": {
            "readOnlyHint": not write,
            "destructiveHint": write,
            "idempotentHint": False,
        },
        "readOnly": not write,
    }, values


def rows(value):
    require(isinstance(value, list), "Invalid provider rows.", code="invalid_provider_response")
    value = value[0] if len(value) == 1 and isinstance(value[0], list) else value
    require(
        len(value) <= 100 and all(isinstance(v, dict) for v in value),
        "Invalid bounded provider rows.",
        code="invalid_provider_response",
    )
    bounded_json(value, 480 * 1024)
    return value


def execute(name, args, config, context, call):
    a = validate(name, args, config)
    schema = config["schema"]
    collection = a["collection"]
    table = q(schema) + "." + q(collection)
    index = collection + "_embedding_hnsw"

    def run(sql, params=(), write=False):
        return rows(call(*tool(sql, params, write)))

    def metadata():
        result = run(
            """SELECT a.atttypmod AS dimensions, CASE op.opcname WHEN 'vector_cosine_ops' THEN 'cosine' WHEN 'vector_l2_ops' THEN 'l2' WHEN 'vector_ip_ops' THEN 'inner_product' END AS metric,
        (i.indisvalid AND i.indisready AND am.amname='hnsw' AND i.indnatts=1 AND i.indkey[0]=a.attnum AND i.indpred IS NULL AND i.indexprs IS NULL) AS valid
        FROM pg_catalog.pg_class t JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attname='embedding' AND NOT a.attisdropped
        JOIN pg_catalog.pg_type ty ON ty.oid=a.atttypid AND ty.typname='vector'
        JOIN pg_catalog.pg_index i ON i.indrelid=t.oid
        JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid AND ix.relname=$3
        JOIN pg_catalog.pg_am am ON am.oid=ix.relam
        JOIN pg_catalog.pg_opclass op ON op.oid=i.indclass[0]
        WHERE n.nspname=$1 AND t.relname=$2 AND t.relkind='r' LIMIT 2""",
            [schema, collection, index],
        )
        require(
            len(result) == 1
            and result[0].get("valid") is True
            and result[0].get("metric") in METRICS,
            "Collection/index configuration differs or is absent.",
            code="index_configuration_mismatch",
        )
        return result[0]

    if name == "ensure_collection":
        run("CREATE SCHEMA IF NOT EXISTS " + q(schema), write=True)
        # The vector extension is an administrator-owned prerequisite in public.
        run(
            f"""CREATE TABLE IF NOT EXISTS {table} (chunk_id text PRIMARY KEY, document_id text NOT NULL, content text NOT NULL DEFAULT '', metadata jsonb NOT NULL DEFAULT '{{}}', source jsonb NOT NULL DEFAULT '{{}}', embedding public.vector({a["dimensions"]}) NOT NULL,
          CHECK (octet_length(chunk_id)<=256 AND octet_length(document_id)<=256 AND octet_length(content)<=65536 AND pg_column_size(metadata)<=32768 AND pg_column_size(source)<=32768 AND jsonb_typeof(metadata)='object' AND jsonb_typeof(source)='object'))""",
            write=True,
        )
        run(
            f"CREATE INDEX IF NOT EXISTS {q(index)} ON {table} USING hnsw (embedding public.{METRICS[a['metric']][0]})",
            write=True,
        )
        run(
            f"CREATE INDEX IF NOT EXISTS {q(collection + '_document_idx')} ON {table} (document_id)",
            write=True,
        )
        meta = metadata()
        require(
            meta["dimensions"] == a["dimensions"] and meta["metric"] == a["metric"],
            "Existing collection dimension or metric differs.",
            code="index_configuration_mismatch",
        )
        return {"records": [meta], "count": 1, "applied": True}
    meta = metadata()
    if name == "get_collection":
        result = [meta]
    elif name == "drop_collection":
        result = run("DROP TABLE " + table, write=True)
    else:
        vectors = [a["vector"]] if "vector" in a else []
        if name == "upsert_document":
            vectors = [c["vector"] for c in a["chunks"]]
        require(
            all(len(v) == meta["dimensions"] for v in vectors),
            "Vector dimension differs from collection.",
            code="invalid_vector",
        )
        projection = "chunk_id, document_id, content, metadata, source" + (
            ", embedding::text AS vector" if a.get("include_vector", False) else ""
        )
        if name in {"upsert_chunk", "upsert_document"}:
            chunks = [a] if name == "upsert_chunk" else a["chunks"]
            batch = [
                {
                    "chunk_id": c["chunk_id"],
                    "document_id": a["document_id"],
                    "content": c.get("content", ""),
                    "metadata": c.get("metadata", {}),
                    "source": a.get("source", {}),
                    "embedding": json.dumps(c["vector"], allow_nan=False),
                }
                for c in chunks
            ]
            result = run(
                f"INSERT INTO {table} (chunk_id,document_id,content,metadata,source,embedding)               SELECT chunk_id,document_id,content,metadata,source,embedding::public.vector FROM jsonb_to_recordset($1::jsonb) AS x(chunk_id text,document_id text,content text,metadata jsonb,source jsonb,embedding text)               ON CONFLICT (chunk_id) DO UPDATE SET document_id=excluded.document_id,content=excluded.content,metadata=excluded.metadata,source=excluded.source,embedding=excluded.embedding RETURNING chunk_id,document_id",  # nosec B608
                [json.dumps(batch, allow_nan=False)],
                True,
            )
        elif name == "get_chunk":
            result = run(
                f"SELECT {projection} FROM {table} WHERE chunk_id=$1 LIMIT 1",  # nosec B608
                [a["chunk_id"]],
            )
        elif name == "update_chunk":
            keys = [k for k in ("content", "metadata", "source", "vector") if k in a]
            params = [a["chunk_id"]]
            assignments = []
            for k in keys:
                params.append(json.dumps(a[k], allow_nan=False) if k != "content" else a[k])
                assignments.append(
                    ("embedding" if k == "vector" else k)
                    + "=$"
                    + str(len(params))
                    + (
                        "::public.vector"
                        if k == "vector"
                        else "::jsonb"
                        if k in {"metadata", "source"}
                        else "::text"
                    )
                )
            result = run(
                f"UPDATE {table} SET "  # nosec B608
                + ",".join(assignments)
                + " WHERE chunk_id=$1 RETURNING chunk_id,document_id",
                params,
                True,
            )
        elif name == "delete_chunk":
            result = run(
                f"DELETE FROM {table} WHERE chunk_id=$1 RETURNING chunk_id",  # nosec B608
                [a["chunk_id"]],
                True,
            )
        elif name == "delete_document":
            # Count only: deleting a document may remove many chunks but never returns them all.
            result = run(
                f"WITH deleted AS (DELETE FROM {table} WHERE document_id=$1 RETURNING 1) SELECT count(*)::int AS deleted_count FROM deleted",  # nosec B608
                [a["document_id"]],
                True,
            )
        elif name == "query":
            operator = METRICS[meta["metric"]][1]
            distance = f"(embedding OPERATOR(public.{operator}) $1::public.vector)"
            score = {
                "cosine": "1-" + distance,
                "l2": "1/(1+" + distance + ")",
                "inner_product": "-" + distance,
            }[meta["metric"]]
            result = run(
                f"SELECT * FROM (SELECT {projection}, {score} AS score FROM {table} WHERE metadata @> $2::jsonb ORDER BY {distance} LIMIT $3::int) AS matches WHERE ($4::text = '' OR score >= NULLIF($4, '')::float8) ORDER BY score DESC",  # nosec B608
                [
                    json.dumps(a["vector"]),
                    json.dumps(a.get("metadata_filter", {})),
                    a["limit"],
                    a.get("min_score", "") if "min_score" in a else "",
                ],
            )
    for row in result:
        if isinstance(row.get("vector"), str):
            row["vector"] = json.loads(row["vector"])
    return {"records": result, "count": len(result), "applied": name in WRITES}
