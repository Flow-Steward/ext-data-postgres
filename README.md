# PostgreSQL

Standalone Flow Steward extension, version 1.4.2. Install only this database package. The extension identifier remains `flowsteward.data-connector-mcp-toolbox-postgres` for installed upgrades.

Supports profiles: `postgres`, `alloydb-omni`, `alloydb-postgres`, `cloud-sql-postgres`. Read-only datasets support schema inspection, validation, bounded previews and saved-query execution. Workflows support one bound INSERT, UPDATE or DELETE, with project connection references and durable effect claims. No triggers are exposed. Public SQL writes reject DDL; fixed vector actions can create or drop their collection schema objects.

Direct connections use host, port, database and user, with password in project secrets. Managed profiles preserve their project, region, instance, IP type and (for AlloyDB) cluster settings. Managed PostgreSQL profiles may use inherited application-default credentials and optional database user/password. The checked-in pinned prebuilt definitions under `scripts/upstream-prebuilts/` document exact environment names and defaults. Configuration forms contain only this database’s fields.

SQL is a deliberately strict subset: raw backslashes and dollar characters are rejected before parsing; pass those characters through bound parameter values. Dataset runtime reads use the host-approved row limit; previews are capped at 100 rows. SQL transport output is bounded to 1 MiB. Writes have no automatic retry; an uncertain result is reported as unknown. Test mode suppresses writes before driver invocation.

The bundled AMD64 and ARM64 native executables are rebuilt from the pinned upstream source with bound SQL parameters and source-specific vector transport limits. `toolbox-provenance.json`, `SHA256SUMS` and `THIRD_PARTY.yml` record both patches, binary checksums and the build image. No other database adapter is installed. The common engine exists only as a pinned build dependency; `scripts/build_toolbox.sh` accepts no database selector and compiles only this package’s profiles.

Build: `scripts/build_toolbox.sh`. Package: `python3 scripts/package_extension.py --runtime-target linux-amd64` and `linux-arm64`. Development tests run through Flow Steward’s Docker extension gate. No live cloud account verification is claimed.

Credential validation: Password is required for postgres. Supply password or the matching profile-specific secret env variable. Managed PostgreSQL/AlloyDB IAM profiles keep password optional; public config env is not a credential source. Required profiles reject missing/blank secrets before provider dispatch, including workflow test mode. The secret environment alternative belongs in project secrets, for example `env: {PROFILE_PASSWORD: ...}` using the exact variable from the selected pinned profile. Wrong-profile environment keys do not satisfy the requirement.

The manifest egress declaration identifies the direct project host field for administrator information. Managed profiles also contact Google control-plane/credential services and resolved database endpoints; this declaration is not an exhaustive destination inventory or an enforced SQL network boundary.

## pgvector on the same PostgreSQL connection

Version 1.4.0 adds vector actions to the existing `toolbox_sql_writer` project connection; no separate pgvector extension or connection is needed. Use the direct `postgres` profile with its existing `host`, `port`, `database`, `user`, and secret `password` (or secret `env.POSTGRES_PASSWORD`). A server administrator must first install pgvector in `public` (`CREATE EXTENSION vector`). The connector never installs server extensions. Managed Cloud SQL/AlloyDB profiles keep their SQL features; vector actions require the direct PostgreSQL profile.

Optional connection settings are `schema` (default `fs_vectors`), `tls` (default true), and `request_timeout_seconds` (1–30, default 15). Vectors use a closed transport configuration; remove public `env` or `query_params` overrides and configure TLS/timeouts using these fields. Public endpoints require certificate and hostname verification. Private endpoints need host administrator `FS_ALLOW_PRIVATE_REMOTE_URLS=true`; only private endpoints may disable TLS. Resolved IP addresses are pinned in the native dialer while preserving the original TLS hostname; fallback endpoints are disabled.

Actions: `ensure_collection`, `get_collection`, `drop_collection` (explicit `confirm=true`), `upsert_chunk`, `upsert_document`, `get_chunk`, `update_chunk`, `delete_chunk`, `delete_document`, and `query`. Each takes the existing `connection_ref` and a collection identifier. Collections are tables with HNSW and document indexes; dimensions range from 1 to 2000 and metrics are cosine, L2, or inner product. Document upserts accept 1–100 chunks atomically in one INSERT. Re-upserting a document updates supplied chunks; omitted chunks remain. A multi-statement collection ensure may partially apply; inspect the schema before retrying a failure.

Queries accept a finite nonzero vector, limit 1–100 (default 20), JSON metadata containment filter, optional minimum score and optional returned vectors. Scores are `1-distance` for cosine, `1/(1+distance)` for L2 and dot product for inner product. HNSW search is approximate; selective metadata filters may return fewer matches. The connector does not generate embeddings.

Vector writes require the host's durable `external_effect_id` claim and are suppressed before network/native I/O in test mode. Claims are host-managed, without database-side claim deduplication. A failed dispatched write reports an uncertain effect and is never automatically retryable. One action deadline covers all metadata and write/search calls. Connection/schema/metric/dimension checks run before the relevant mutation.

Vector input is capped at 256 KiB and inline result JSON at 480 KiB. Managed vector source instances additionally bound native results to 100 rows/480 KiB, PostgreSQL message bodies to 512 KiB and connection reads to 2 MiB. Ordinary SQL/dataset sources retain their existing host-approved row limits. Values remain driver-bound, SQL identifiers follow a closed ASCII grammar, and fixed internal vector DDL does not widen the public SQL guard.

AMD64 and ARM64 archives each contain exactly one native executable serving both SQL and vectors. See `toolbox-provenance.json` for the combined native patches and hashes, and [BRAND-ASSETS.md](BRAND-ASSETS.md) for logo provenance.
