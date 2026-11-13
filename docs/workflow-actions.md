# PostgreSQL workflow actions

Use a **Dataset** when a workflow needs to read rows using a saved SELECT query.
Dataset queries in this connector are read-only. To change database rows, choose
**SQL: Insert, update or delete rows**. Choose the **Vector:** actions when building
a searchable knowledge base in PostgreSQL with pgvector.

Action names below are the names shown in the workflow builder. The IDs in
parentheses identify existing actions and remain compatible with saved workflows.

## Write workflow results to PostgreSQL

1. Configure a PostgreSQL project connection with the database host, port,
   database, user and credentials in connection secrets. Grant that user the
   permissions required by your SQL statement.
2. Add **SQL: Insert, update or delete rows** (`execute_write`) to the workflow
   and select that connection.
3. Enter one statement in **SQL write statement**. Use named parameters for values:

   ```sql
   UPDATE orders SET status = :status WHERE order_id = :order_id
   ```

4. Set **Parameters (JSON)**, binding values from previous steps as needed:

   ```json
   {"status":"approved","order_id":1042}
   ```

5. Set **Parameter types (JSON Schema)** to declare a type for every SQL placeholder before execution:

   ```json
   {"type":"object","properties":{"status":{"type":"string"},"order_id":{"type":"integer"}},"required":["status","order_id"]}
   ```

The type declaration is required when the SQL contains named placeholders; omit it only for a statement without parameters. Supported parameter types are string, integer, number, boolean and null.

The same action supports a single INSERT or DELETE. For an upsert, use PostgreSQL
`INSERT ... ON CONFLICT ... DO UPDATE`, with a matching unique constraint in your
database. It does not accept multi-statement scripts, DDL or SELECT reads.
`result.status` reports execution; `result.rows` contains returned rows when the
statement returns them. `result.returned_row_count` counts returned rows, not all
affected rows. Test mode validates the action and suppresses the write. If a
connection fails after dispatch, inspect the database before repeating the action:
the server may already have committed it.

## Prepare vector storage

Use an existing connection with the **direct `postgres` profile**. A database
administrator must install pgvector in `public` first. The vector actions do not
install it and do not support the managed AlloyDB/Cloud SQL profiles.

The connection's vector settings include a schema (default `fs_vectors`), TLS
verification (default enabled) and request timeout. Grant its database user access
to that schema and the collection tables; creating a collection also requires
permission to create its schema/table/indexes. A private database endpoint requires
the Flow Steward administrator to permit private remote URLs. See the connection
and transport requirements in the repository README.

A **collection** is a dedicated table and search index for fragments. A **document**
groups fragments with the same Document ID. A **fragment** stores text, an embedding
and optional metadata/source objects. Fragment IDs are unique across a collection,
so prefix them with the document ID, for example `manual_42_part_1`.

The extension does not split documents or generate embeddings. Use earlier steps
for those operations. Generate document and question embeddings with the same
model and use a collection whose dimensions match that model's output (1–2000).

## Index a document, then search it

1. Run **Vector: Create or check a collection** (`ensure_collection`) with collection
   `knowledge_base`, dimensions matching your embedding model and metric `cosine`.
   For example, set dimensions to 1536 only for a model that returns 1536 values.
   This creates the schema/table/indexes if missing and checks existing dimensions
   and metric. It does not migrate an incompatible existing collection. Setup uses
   multiple statements and can partially apply on failure; inspect it before retrying.
2. Split a document into text fragments and generate an embedding for each.
3. Run **Vector: Save document fragments and embeddings** (`upsert_document`).
   Select the same connection/collection, set Document ID to `manual_42`, and supply
   a **Document fragments** array like the following. The three-value vector is
   illustrative and only fits a 3-dimensional collection; use your model's actual
   full-length vector in a real workflow.

   ```json
   [{"chunk_id":"manual_42_part_1","content":"Refund requests can be submitted within 30 days.","vector":[0.1,0.2,0.3],"metadata":{"language":"en","category":"refunds"}}]
   ```

   Optionally set **Document source** to `{"url":"https://example.com/manual"}`.
   One call accepts 1–100 fragments. Reusing a Fragment ID replaces that fragment;
   existing fragments omitted from the array remain stored. To remove obsolete
   fragments, delete them explicitly or delete the document before reindexing.
4. Generate an embedding of the user's question with the same embedding model.
5. Run **Vector: Search similar text** (`query`): select the connection/collection,
   pass the numeric array into **Question or search-text embedding**, and set
   **Maximum matching fragments** to 5. To restrict language, use
   **Metadata filter** `{"language":"en"}`.
6. Pass the matching fragments from `result.records`, including their text, source
   and scores, to the step that generates an answer. Search itself does not answer
   the question. Returned results may be fewer than the requested limit because
   search is approximate and applies filters.

Leave **Minimum similarity score** empty while getting started. Scores depend on
the collection metric: cosine uses `1-distance`, L2 uses `1/(1+distance)`, and inner
product uses the dot product. They are not universal confidence percentages.

## Inspect, update and remove stored content

Every action selects the existing direct PostgreSQL connection and collection.

| Action in the builder | Example and behavior |
| --- | --- |
| **Vector: Inspect a collection** (`get_collection`) | Inspect `knowledge_base` to check dimensions, metric and index validity. Returns configuration, not a document listing or total count. |
| **Vector: Save text fragment and embedding** (`upsert_chunk`) | Save `manual_42_part_1` under Document ID `manual_42`, with text, embedding and optional metadata/source. An existing Fragment ID is replaced; omitted optional values become empty defaults. |
| **Vector: Read a saved text fragment** (`get_chunk`) | Read `manual_42_part_1` to inspect its text and metadata. Enable **Include embedding in results** only if the numeric array is needed. |
| **Vector: Update a text fragment** (`update_chunk`) | Set Fragment ID `manual_42_part_1` and supply new text and its new embedding. Provide at least one changed field. Omitted fields are preserved; supplied metadata/source objects replace the old objects. It does not insert a missing fragment. |
| **Vector: Delete one text fragment** (`delete_chunk`) | Remove obsolete `manual_42_part_1` while preserving other fragments of `manual_42`. |
| **Vector: Delete all fragments of a document** (`delete_document`) | Remove every fragment of `manual_42` before replacing the document. The original file and collection table remain. Read the number deleted from `result.records[0].deleted_count`. |
| **Vector: Delete a collection and all its text** (`drop_collection`) | Remove a temporary collection such as `import_trial`, its indexes and every stored fragment. Explicitly enable **Confirm deletion**. |

Vector write actions are suppressed in workflow test mode. Read/search actions
still read the database. Writes that may already have reached the database are
not automatically safe to repeat after a transport failure.
