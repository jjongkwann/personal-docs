# SQLite Graph RAG

## Purpose and Scope

**Purpose**: answer questions at the level of *"how are the concepts across my whole corpus connected?"*. It **complements** the existing RRF + reranker search where that search is weak — it does not replace it.

**Core design**: extraction is explicit. Claude Code can read chunks with `graph_list_chunks`, extract concepts/relations, and store them via `graph_store_concepts`; `pkb graph rebuild-evidence-local` can run the same pending loop with a local Ollama model. Native `graph_explain`, `graph_path`, `graph_query`, and `graph_affected` queries read SQLite directly; `graph_map` renders an offline HTML view.

| | Existing (ES + RRF + reranker) | Graph RAG |
|---|---|---|
| Good at | **Specific queries** like "what is DI?", "what's the BM25 formula?" | **Relational/global queries** like "how are DI, IoC, Bean, and Container tangled together?" |
| Data unit | Chunk (500 tokens) | Concept (entity) + relation |
| Response material | Body chunks | SQLite concepts, relations, curated prose, and source evidence |
| Build trigger | Immediately on ingest | Explicit MCP extraction or local Ollama rebuild |

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
| `extracted_chunks` | Extraction-complete markers `(doc_id, chunk_index)` → `content_hash`, `input_hash` |
| `graph_meta` | Key/value markers — one-shot schema migrations and the `edge_evidence_rebuild` staging flag |

Incremental extraction is driven by `extracted_chunks`. A chunk is pending when either marker hash
differs from the current ES source. `content_hash` tracks the body; `input_hash` also covers
`doc_id`, `chunk_index`, title, section path, and category, so attribution changes trigger
re-extraction. Re-extraction **replaces that chunk's mentions and relation evidence** — concepts
and relations that disappeared from a revised chunk don't linger. Existing markers without
`input_hash` (including legacy rows with NULL `chunk_index`) and graph data are preserved, but
those chunks remain pending until extracted against the current input.
Edge `weight`/`evidence_count` are computed from the actual row count in `concept_edge_evidence`,
so re-running the same chunk doesn't inflate them. When a document is deleted, a chunk moves, or
content changes, the corresponding evidence is cleaned up too.

Schema definitions live in `src/pkb/graph/schema.py`, CRUD in `src/pkb/graph/store.py`.

### Concept Normalization (dedup)
Extraction reuses an existing concept on a normalized slug match within its category/namespace
(`dependency injection` == `Dependency Injection`). It records proposed aliases but reports
conflicts instead of silently joining concepts. Automatic extraction deliberately disables alias
and embedding-similarity matching to avoid merging unrelated abbreviations or homonyms. Review
remaining duplicates and merge them explicitly. `mention_count` is recomputed from actual
`concept_mentions` rows, so re-extracting the same chunk does not inflate it.

Duplicates that passed dedup but still ended up split into separate nodes (notation variants,
etc.) are merged after the fact with `store.merge_concepts(conn, winner_slug, loser_slugs)` —
edges, mentions, aliases, and prose are transferred to the winner, and loser rows are deleted in
the same SQLite transaction. Note: **components** of a broader concept, like "MCP
Server", are not notation variants and must not be merged.

### Curation Criteria
Search-result concept attachments prefer concepts that are **`concept_curation.label='real'` and
have at least one relation (edge)**. Orphan concepts remain in SQLite but are omitted from those
attachments because they add little navigational value. `vocab` concepts are also excluded from
default graph seeding. If the curation table is empty, the system falls back to all concepts.

### Relation Edge Aggregation
- Re-running the same chunk's (src, dst, relation) → evidence upsert is idempotent (count unchanged)
- The same relation across different chunks → `weight`/`evidence_count` aggregate the evidence count
- Each mentioning chunk → recorded in `concept_mentions`

---

## Pipeline: Self-Extraction → Storage → Native Query

### 1. `graph_list_chunks(category|doc_id, offset, limit, pending_only)`
Returns ES chunks as paginated JSON, each with `content_hash` and `input_hash`. With
`pending_only=True`, unextracted or changed-input chunks are returned. Claude Code reads this
result directly and extracts concepts/relations using the rules below. Before extracting, check
the existing vocabulary with `graph_list_concepts` and reuse existing names/slugs for overlapping concepts.

- Concepts: concrete noun phrases (e.g. "Dependency Injection", "BM25"). Excludes generic words, personal names, place names
- Relation types: `related_to` | `part_of` | `prerequisite_of` | `example_of` (free-form labels allowed when needed)
- Up to 8 concepts and 12 relations per chunk

### 2. `graph_store_concepts(items_json)`
Each item must echo both hashes from its listed chunk, even when `concepts` is empty. Storage
checks them against the current ES chunk before changing SQLite; missing, deleted, or changed
inputs are rejected per item and reported, while valid items in the batch are stored. Accepted
items upsert concepts/relations, aliases, mentions, edges, and extraction markers.

### 3. Querying and Reading
Use `graph_explain` for one concept, `graph_path` for a shortest path, `graph_query` for a
semantic-seeded bounded subgraph, and `graph_affected` for stored-direction downstream traversal.
Every returned edge includes confidence and bounded `doc_id`/`chunk_index` evidence. Use
`graph_map` for a human-readable offline HTML view and `search_knowledge` when the answer requires
source text rather than graph structure.

---

## CLI Helper Commands

```bash
uv run pkb graph stats        # concept/edge/evidence/mention/document/alias stats
uv run pkb graph explain "BM25"  # one concept with inbound/outbound evidence
uv run pkb graph path "BM25" "RRF"  # bounded shortest path
uv run pkb graph query "how do lexical and vector retrieval connect?"  # semantic-seeded subgraph
uv run pkb graph affected "BM25" --relation prerequisite_of  # stored-direction downstream traversal
uv run pkb graph map --concept "BM25"   # offline Evidence Map HTML snapshot
uv run pkb graph reset-evidence --yes  # keep the existing graph, reset staging evidence/markers
uv run pkb graph rebuild-evidence-local --yes  # extract all pending with a local Ollama model
uv run pkb graph finalize-evidence --yes  # after confirming full extraction, atomically switch to the staging graph
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

### Evidence Map (`graph map`)

`graph map` renders a **single self-contained HTML file** — a radial-tree snapshot of a subgraph
with an evidence panel and relation filters, no server and no network access required. Pick exactly
one entry mode:

```bash
uv run pkb graph map --concept "BM25"                    # centered on one concept
uv run pkb graph map --query "how do BM25 and RRF relate?"  # semantic-seeded
uv run pkb graph map --path BM25 RAG                     # shortest path between two concepts
```

| Option | Default | Meaning |
|---|---|---|
| `--depth` | `1` | Expansion hops (0–2) |
| `--max-nodes` | `30` | Node cap (1–100) |
| `--relation` / `-r` | all | Comma-separated relation-type filter |
| `--evidence-limit` | `5` | Evidence rows per relation (0–20) |
| `--out` | `<GRAPH_DB_PATH dir>/evidence-map.html` | Output path |
| `--open` | off | Open in the browser after rendering |

Specifying zero or more than one of `--concept`/`--query`/`--path` is a usage error. `--query`
embeds the text, so it loads the embedding model; `--concept` and `--path` read SQLite only.
Implementation: `src/pkb/graph/viewmap.py`.

There's no batch API build or export CLI. Building remains MCP-first; reading is available through
native SQLite graph queries, the CLI, and the offline Evidence Map.

---

## Why SQLite

- Single file, no install required, easy to back up
- Fast enough at scales of thousands to tens of thousands of nodes
- Using just Python's `sqlite3` keeps external dependencies to a minimum

## Why We Dropped API Extraction

Claude Code already has a session that can read chunks, so a separate LLM API call (with its cost and key management) was redundant. With just two tools, `graph_list_chunks`/`graph_store_concepts`, the Claude Code session itself acts as the extractor.
