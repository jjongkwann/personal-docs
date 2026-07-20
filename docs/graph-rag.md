# SQLite Graph RAG

## Purpose and Scope

**Purpose**: answer questions at the level of *"how are the concepts across my whole corpus connected?"*. It **complements** the existing RRF + reranker search where that search is weak — it does not replace it.

**Core design**: the concept layer is 100% self-extracted by Claude Code — no API calls, no LLM cost. Claude Code reads chunks with `graph_list_chunks`, extracts concepts/relations itself, and stores them in SQLite via `graph_store_concepts`. Native `graph_explain`, `graph_path`, `graph_query`, and `graph_affected` queries read that SQLite graph directly, while `sync_concept_notes` projects it into `data/_concepts/<slug>.md` vault notes for human browsing.

| | Existing (ES + RRF + reranker) | Graph RAG |
|---|---|---|
| Good at | **Specific queries** like "what is DI?", "what's the BM25 formula?" | **Relational/global queries** like "how are DI, IoC, Bean, and Container tangled together?" |
| Data unit | Chunk (500 tokens) | Concept (entity) + relation |
| Response material | Body chunks | `data/_concepts/` notes (prose + relation links + sources) |
| Build trigger | Immediately on ingest | Explicit run via MCP `graph_list_chunks`/`graph_store_concepts` |

**Not doing**: automatic full-graph builds (opt-in only), a full graph DB like Neo4j, GNN embeddings, automatic updates driven by conversation history.

---

## Storage Structure

**File location**: `data/.graph/pkb_graph.sqlite` (gitignored)

Main tables:

| Table | Role |
|--------|------|
| `concepts` | Normalized concept nodes (name, slug, description, embedding, mention_count) |
| `concept_aliases` | Aliases like DI → Dependency Injection |
| `documents` | Document nodes linked to ES `doc_id` |
| `concept_edges` | Aggregated projection of relations between concepts (relation, weight, evidence_count) |
| `concept_edge_evidence` | Evidence per `doc_id`/`chunk_index` that a relation was extracted from |
| `concept_mentions` | `doc_id`/`chunk_index` where a concept appears |
| `concept_curation` | Concept curation (real/vocab) + distilled prose |
| `extracted_chunks` | Extraction-complete markers `(doc_id, chunk_index)` → `content_hash` |

Incremental extraction is driven by `extracted_chunks`. If a chunk's current `content_hash`
differs from the marker for that index (i.e. content changed or it moved to a different index),
it becomes pending and gets re-extracted, and re-extraction **replaces that chunk's mentions and
relation evidence** — concepts/relations that disappeared from a revised chunk don't linger.
Rows with a NULL `chunk_index` are legacy markers recorded by hash only, and are honored as a
fallback only until that chunk's content changes.
Edge `weight`/`evidence_count` are computed from the actual row count in `concept_edge_evidence`,
so re-running the same chunk doesn't inflate them. When a document is deleted, a chunk moves, or
content changes, the corresponding evidence is cleaned up too.

Schema definitions live in `src/pkb/graph/schema.py`, CRUD in `src/pkb/graph/store.py`.

### Concept Normalization (dedup)
1. **Slug match**: `dependency injection` == `Dependency Injection`
2. **Alias match**: "DI" maps to the existing "Dependency Injection" concept
3. **Embedding similarity** (≥ `GRAPH_DEDUP_THRESHOLD`, default 0.88): semantic match to an existing concept → merge
4. Insert if new, otherwise add an alias (`mention_count` is recomputed from actual
   `concept_mentions` rows — re-extracting the same chunk doesn't inflate it)

Duplicates that passed dedup but still ended up split into separate nodes (notation variants,
etc.) are merged after the fact with `store.merge_concepts(conn, winner_slug, loser_slugs)` —
edges, mentions, aliases, and prose are transferred to the winner, and loser notes are cleaned up
as orphans on the next `sync_concept_notes`. Note: **components** of a broader concept, like "MCP
Server", are not notation variants and must not be merged.

### Note Projection Criteria
`sync_concept_notes` doesn't project every concept into a note — only concepts that are
**`concept_curation.label='real'` (classified as a real concept) and have at least one relation
(edge)** get a note. Orphan concepts (zero relations) have no connective value and are excluded,
though they remain in SQLite (`store.projected_slugs`). If the curation table is empty, it falls
back to projecting everything. Relations and prose links pointing to unprojected concepts render
as plain text so they don't produce broken wikilinks.

### Relation Edge Aggregation
- Re-running the same chunk's (src, dst, relation) → evidence upsert is idempotent (count unchanged)
- The same relation across different chunks → `weight`/`evidence_count` aggregate the evidence count
- Each mentioning chunk → recorded in `concept_mentions`

---

## Pipeline: Self-Extraction → Storage → Note Projection

### 1. `graph_list_chunks(category|doc_id, offset, limit, pending_only)`
Returns ES chunks as paginated JSON. With `pending_only=True`, only unextracted or content-changed chunks are returned (incremental extraction). Claude Code reads this result directly and extracts concepts/relations using the rules below. Before extracting, check the existing vocabulary with `graph_list_concepts` and reuse existing names/slugs for overlapping concepts.

- Concepts: concrete noun phrases (e.g. "Dependency Injection", "BM25"). Excludes generic words, personal names, place names
- Relation types: `related_to` | `part_of` | `prerequisite_of` | `example_of` (free-form labels allowed when needed)
- Up to 8 concepts and 12 relations per chunk

### 2. `graph_store_concepts(items_json)`
Upserts the extracted concept/relation JSON into SQLite (including normalization, aliases, mentions, edges).

### 3. `sync_concept_notes(confirm_prune)`
One-way, deterministic, idempotent projection from SQLite to `data/_concepts/<slug>.md` notes. Concept edges are rewritten as `[[wikilinks]]` so Obsidian's graph view can render the concept graph. Link targets are **vault physical paths**, not doc_ids (e.g. `[[PKB/_concepts/slug|name]]`) — if `DATA_ROOT` is outside the vault, it falls back to filename-only links. Relations to unprojected (vocab) concepts are shown as plain text. Only the region between `<!-- pkb:auto:start/end -->` markers is regenerated, so user prose outside the markers is preserved. There's no note-to-SQLite promotion path (SQLite is always the SSOT). It also renders `_concepts/index.md`, a MOC that groups projected concepts by category — the entry point into the concept vocabulary catalog. Implementation: `src/pkb/graph/notes.py`.

### 4. Querying and Reading
Use `graph_explain` for one concept, `graph_path` for a shortest path, `graph_query` for a
semantic-seeded bounded subgraph, and `graph_affected` for stored-direction downstream traversal.
Every returned edge includes confidence and bounded `doc_id`/`chunk_index` evidence. The projected
`data/_concepts/<slug>.md` notes remain the human-readable Obsidian view. Concept notes aren't indexed
in ES, so use `search_knowledge` when the answer requires source text rather than graph structure.

---

## CLI Helper Commands

```bash
uv run pkb graph stats        # concept/edge/evidence/mention/document/alias stats
uv run pkb graph explain "BM25"  # one concept with inbound/outbound evidence
uv run pkb graph path "BM25" "RRF"  # bounded shortest path
uv run pkb graph query "how do lexical and vector retrieval connect?"  # semantic-seeded subgraph
uv run pkb graph affected "BM25" --relation prerequisite_of  # stored-direction downstream traversal
uv run pkb graph reset-evidence --yes  # keep the existing graph, reset staging evidence/markers
uv run pkb graph rebuild-evidence-local --yes  # extract all pending with a local Ollama model
uv run pkb graph finalize-evidence --yes  # after confirming full extraction, atomically switch to the staging graph
uv run pkb graph sync-notes   # sync SQLite → data/_concepts/ notes
uv run pkb graph sync-notes --yes   # skip confirmation for bulk cleanup (21+ items)
```

To migrate legacy append-only edges to evidence-based ones, first run `reset-evidence --yes` once,
then re-run the `graph_list_chunks(..., pending_only=True)` → `graph_store_concepts` loop over all
chunks. During the rebuild, existing relations/mentions keep being served while new evidence
accumulates in staging. Once pending reaches 0, running `finalize-evidence --yes` replaces the
existing relations with the evidence aggregate in one shot. Concepts, aliases, and curated prose
are preserved throughout.

If Ollama has a generation model installed, `rebuild-evidence-local` automates this loop with
structured output. The default model is `gpt-oss:20b`, the default batch size is 8 chunks, and it
resumes from the markers if interrupted. Progress is logged to
`data/.logs/graph-evidence-rebuild.jsonl`. Use `--max-batches 1` for a sample validation run, and
the default `--max-batches 0` for a full run.

There's no batch API build or export CLI. Building remains MCP-first; reading is available through
native SQLite graph queries, the CLI, and projected Obsidian notes.

---

## Why SQLite

- Single file, no install required, easy to back up
- Fast enough at scales of thousands to tens of thousands of nodes
- Using just Python's `sqlite3` keeps external dependencies to a minimum

## Why We Dropped API Extraction

Claude Code already has a session that can read chunks, so a separate LLM API call (with its cost and key management) was redundant. With just two tools, `graph_list_chunks`/`graph_store_concepts`, the Claude Code session itself acts as the extractor.
