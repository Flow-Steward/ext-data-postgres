# PostgreSQL

Standalone Flow Steward extension, version 1.3.0. Install only this database package. The extension identifier remains `flowsteward.data-connector-mcp-toolbox-postgres` for installed upgrades.

Supports profiles: `postgres`, `alloydb-omni`, `alloydb-postgres`, `cloud-sql-postgres`. Read-only datasets support schema inspection, validation, bounded previews and saved-query execution. Workflows support one bound INSERT, UPDATE or DELETE, with project connection references and durable effect claims. No triggers or DDL are exposed.

Direct connections use host, port, database and user, with password in project secrets. Managed profiles preserve their project, region, instance, IP type and (for AlloyDB) cluster settings. Cloud PostgreSQL/MySQL profiles may use inherited application-default credentials and optional database user/password; Cloud SQL MSSQL requires database credentials. The checked-in pinned prebuilt definitions under `scripts/upstream-prebuilts/` document exact environment names and defaults. Configuration forms contain only this database’s fields.

SQL is a deliberately strict subset: raw backslashes and dollar characters are rejected before parsing; pass those characters through bound parameter values. Reads return at most 100 rows and 480 KiB. Writes have no automatic retry; an uncertain result is reported as unknown. Test mode suppresses writes before driver invocation.

The bundled AMD64 and ARM64 native executables are unchanged, checksum-verified minimal builds from the prior database-specific package. `toolbox-provenance.json`, `SHA256SUMS` and `THIRD_PARTY.yml` retain the pinned upstream source, original binding patch and build image. No other database adapter is installed. The common engine exists only as a pinned build dependency; `scripts/build_toolbox.sh` accepts no database selector and compiles only this package’s profiles.

Build: `scripts/build_toolbox.sh`. Package: `python3 scripts/package_extension.py --runtime-target linux-amd64` and `linux-arm64`. Development tests run through Flow Steward’s Docker extension gate. No live cloud account verification is claimed.
