# Changelog

## 1.4.7

- Move the stateless test launcher under `tests/` so installation correctly separates test-only import setup from extension runtime code. PostgreSQL queries, writes, pgvector operations and native payloads are unchanged.

## 1.4.4

- Rebuild with checksum-verified official Go 1.27.4 SDK security fixes. The pinned Go 1.27.3 image supplies only the bootstrap environment; Toolbox source, all 106 module pins and safety patches remain unchanged.

## 1.4.3

- Upgrade pinned Toolbox to v1.16.0, Go 1.27.3 and the reviewed dependency baseline. Port bound SQL and native safety checks to current driver interfaces while retaining approved connection profiles.

## 1.4.2

- Rebuild pinned Toolbox v1.3.0 with Go 1.26.10 and security dependency floors, preserving database safety patches and host behavior.

## 1.4.1

- Use the original official provider artwork in the catalog and extension icon. Runtime behavior is unchanged.

## 1.4.0

- Adds pgvector collections, document/chunk lifecycle and similarity search to existing direct PostgreSQL project connections.
- Preserves dataset reads, guarded SQL writes and managed connection profiles.
- Combines bound SQL and opt-in vector transport safety in one native executable per target archive.
- Uses the official PostgreSQL logo with brand attribution.

## 1.3.0

- Publish PostgreSQL as an independent extension and repository.
- Preserve installed IDs, native binaries and read/write behavior.
- Narrow settings, runtime profiles and user-facing pages to this database.
