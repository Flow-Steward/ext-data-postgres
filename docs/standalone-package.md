# Standalone package design and validation

One installable extension owns PostgreSQL, its connection profiles, UI and driver. Existing extension/connection/dataset IDs are stable. Version 1.3.0 supersedes the former family release. Pinned native executables are retained byte-for-byte; build tooling is self-contained and accepts no database selector. No factory/template source is installed.

Implementation: copy the final tested database render; narrow catalog, environment and parameter binding registries; derive field sets from pinned source profiles; retain managed authentication fields; replace generic UI copy; add architecture regression; preserve legal/native provenance; package separate AMD64/ARM64 archives.

Validation: format changed Python files and inspect the source diff, then run extension-static and extension-tests through the disposable Docker gate when the coordinator grants a slot. Compare every archive entry and native hash with the committed standalone source. Cloud connections require separate account-backed verification; no live cloud success is claimed.
