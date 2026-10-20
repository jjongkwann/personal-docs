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
| `concepts` | Normalized concept nodes (name, slug, kind `concept_type`, description, embedding, mention_count) |
| `concept_aliases` | Aliases like DI → Dependency Injection |
| `documents` | Document nodes linked to ES `doc_id` |
| `concept_edges` | Aggregated projection of relations between concepts (relation, weight, evidence_count) |
| `concept_edge_evidence` | Evidence per `doc_id`/`chunk_index` that a relation was extracted from |
| `concept_mentions` | `doc_id`/`chunk_index` where a concept appears |
| `concept_curation` | Concept curation (real/vocab) + distilled prose |
| `extracted_chunks` | Extraction-complete markers `(doc_id, chunk_index)` → `content_hash`, `input_hash` |
| `graph_meta` | Key/value markers — one-shot schema migrations, `tbox_version`, and the `edge_evidence_rebuild` staging flag |

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

## Ontology (TBox)

`src/pkb/graph/ontology.py` defines the classes and relations that every concept and edge must fit.
It is data only (`TBOX_VERSION = "pkb-tbox/1"`, recorded in `graph_meta.tbox_version`). Storage
checks, validation, query-time inference, the extraction prompt, and RDF export all read it.

### Concept Kinds
Each concept has exactly one leaf kind in `concepts.concept_type`, and leaf kinds are mutually
disjoint. NULL marks an unclassified legacy concept. Korean and English one-line definitions live in
`ontology.KINDS`.

| Group | Kinds |
|---|---|
| Abstract | `field`, `problem`, `principle` |
| Method | `technique`, `algorithm`, `pattern` |
| Artifact | `software`, `model`, `dataset`, `protocol` |
| Measure | `metric` |

### Relations

| Relation | Inverse (display) | Characteristics | Signature (src → dst) |
|---|---|---|---|
| `is_a` | `has_subtype` | transitive, irreflexive, acyclic | same kind on both ends |
| `part_of` | `has_part` | transitive, irreflexive, acyclic | same group, or dst is `field` |
| `prerequisite_of` | `requires` | transitive, irreflexive, acyclic | any → any |
| `uses` | `used_by` | irreflexive | Method\|Artifact → Method\|Artifact\|Measure |
| `implements` | `implemented_by` | asymmetric, irreflexive | software\|model → algorithm\|technique\|pattern\|protocol |
| `addresses` | `addressed_by` | irreflexive | Method\|Artifact → problem |
| `alternative_to` | itself | symmetric, irreflexive | same group |
| `related_to` | itself | symmetric, irreflexive | any → any; use only when nothing more specific fits |

`example_of` is no longer in the vocabulary (existing rows migrate to `is_a`), and free-form labels
are rejected. `ontology.describe()` returns the whole TBox as JSON, and `ontology.allowed_relations(src_kind,
dst_kind)` lists the relations whose signature fits a pair of kinds.

### Write-Time Rules
`store.add_edge` checks every relation before writing, so `graph_store_concepts` and the local Ollama
rebuild follow the same rules:

- The type must be in the catalog, and an edge may not point to its own source.
- The signature must hold when both ends have a kind. When either end is unclassified the edge is
  stored and counted as unchecked.
- An acyclic relation rejects an edge whose target already reaches its source over the same relation.
- Symmetric relations are stored once, with `src_id < dst_id`.

The `graph_store_concepts` result lists each rejected relation as one JSON line with `code`
(`unknown_relation`, `self_loop`, `signature`, `cycle`), `reason`, both kinds, and
`allowed_relations`. Other valid items and relations in the payload are still stored. Concepts carry
`kind`: a new concept without a valid kind is rejected (reported with `allowed_kinds`), an existing
unclassified concept takes the payload kind, and an existing concept with a different kind keeps the
stored one and is reported as a warning. Kinds change only through explicit classification.

### Migration
Every graph connection adds `concept_type` if it is missing and records `tbox_version`. Connections
that write also normalize relation keys that do not fit the TBox. It moves `related_to`/`alternative_to` rows stored as
`src_id > dst_id` to the other orientation, merging evidence per chunk with the highest confidence
and recomputing edge aggregates from evidence, and moves `example_of` rows to `is_a`. Only pairs
stored in both directions collapse into one edge. This runs on each connection, not once, because a
process started before this change (a long-running extractor, for example) keeps writing reverse
rows into the same database; the check costs a few milliseconds. Read-only connections skip it and
take no write lock, so graph reads keep working while another process holds a write transaction.
Unknown labels stay in place and appear in the validation report.

### Classification
`services.list_unclassified_concepts(conn, offset=0, limit=50)` pages unclassified concepts by
mention count with name, description, and aliases. `services.classify_concepts(conn, [{"slug",
"kind"}])` sets or changes kinds; one invalid kind rejects the whole batch. It returns the edges that
now violate their signature so they can be fixed.

### Validation
`validate.validate_graph(conn)` returns counts and bounded slug samples.

- **hard**: unknown relation labels, self-loops, signature violations, cycles in acyclic relations
  (as slug paths), symmetric pairs stored in both directions, edges without evidence.
- **soft**: unclassified concepts, unchecked edges, `related_to`/`alternative_to` between concepts
  already linked by an `is_a`/`part_of` path (SKOS integrity condition S27), transitive edges that a
  longer path of the same relation already implies, and the `related_to` share of all edges.

### Inference
Inferred edges are computed at query time. They are never stored and never count as evidence.
`query.transitive_closure(conn, concept, relation, direction="up"|"down")` follows `is_a`,
`part_of`, or `prerequisite_of` and returns only edges that are not asserted directly, each with
`inferred: true`, `hops`, and the premise path `via`. `up` follows the stored direction.
`graph_explain` adds an `inferred` list (outbound closure), `inverse_relation` on inbound edges, and
`kind` on the concept and edge endpoints. Directed traversal (`graph_affected`, directed `graph_path`)
follows symmetric relations in both directions.

### RDF Export
`export.export_rdf(conn, path, fmt="turtle"|"trig", base="urn:pkb:")` writes:

- The TBox as OWL 2: a class per kind under its group and the root class `concept` (a subclass of
  `skos:Concept`), `owl:AllDisjointClasses` over the leaf kinds, one object property per relation with
  its characteristics, `owl:inverseOf` for the inverse labels, domain/range where restricted, and
  `rdfs:subPropertyOf` to `skos:broader` (`is_a` also `isothes:broaderGeneric`, `part_of` also
  `isothes:broaderPartitive`) or `skos:related`. The version IRI is `<base>tbox/1`.
- Every concept as a `skos:Concept` of its kind class in one `skos:ConceptScheme`, with `prefLabel`,
  `altLabel` (aliases), and `definition`, plus the asserted edges.
- With TriG, the inferred transitive closure goes into the named graph `<base>graph/inferred`.

Concept IRIs are `<base>concept/<percent-encoded slug>`; display names are never used. OWL 2 DL allows
irreflexivity only on non-transitive properties, so transitive relations carry the annotation
`pkbo:acyclic true` instead, and the same-kind/same-group conditions are recorded in
`pkbo:signature`.

### Tools and Commands

| MCP tool | CLI | Access | Use |
|---|---|---|---|
| `graph_ontology` | `pkb graph ontology` | read | The TBox as JSON; read it before extraction |
| `graph_inferred` | `pkb graph inferred CONCEPT --relation is_a --direction up` | read | Inferred transitive relations with premise paths |
| `graph_validate` | `pkb graph validate` (exit code 1 on hard violations) | admin | The hard/soft report; `pkb doctor` prints its counts |
| `graph_classify` | — | admin | List unclassified concepts, then assign `[{slug, kind}]` |
| — | `pkb graph export --out graph.ttl [--format trig]` | admin (shell only) | RDF file of the whole graph |

Under an access policy the read tools see only the subgraph backed by readable evidence (see
[Access Control](access-control.md)), so inference never crosses a hidden premise.

---

## Pipeline: Self-Extraction → Storage → Native Query

### 1. `graph_list_chunks(category|doc_id, offset, limit, pending_only)`
Returns ES chunks as paginated JSON, each with `content_hash` and `input_hash`. With
`pending_only=True`, unextracted or changed-input chunks are returned. Claude Code reads this
result directly and extracts concepts/relations using the rules below. Before extracting, check
the existing vocabulary with `graph_list_concepts` and reuse existing names/slugs for overlapping concepts.

- Concepts: concrete noun phrases (e.g. "Dependency Injection", "BM25"). Excludes generic words, personal names, place names
- Each concept carries one `kind` from the TBox (required for new concepts)
- Relation types: the eight TBox relations; pick the most specific one whose signature fits both kinds,
  and use `related_to` only as a fallback
- Up to 8 concepts and 12 relations per chunk

### 2. `graph_store_concepts(items_json)`
Each item must echo both hashes from its listed chunk, even when `concepts` is empty. Storage
checks them against the current ES chunk before changing SQLite; missing, deleted, or changed
inputs are rejected per item and reported, while valid items in the batch are stored. Accepted
items upsert concepts/relations, aliases, mentions, edges, and extraction markers. Concepts and
relations that break the TBox are rejected individually with structured reasons (see Write-Time Rules).

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

There's no batch API build. Building remains MCP-first; reading is available through native SQLite
graph queries, the CLI, the offline Evidence Map, and RDF export (`src/pkb/graph/export.py`).

---

## Why SQLite

- Single file, no install required, easy to back up
- Fast enough at scales of thousands to tens of thousands of nodes
- Using just Python's `sqlite3` keeps external dependencies to a minimum

## Why We Dropped API Extraction

Claude Code already has a session that can read chunks, so a separate LLM API call (with its cost and key management) was redundant. With just two tools, `graph_list_chunks`/`graph_store_concepts`, the Claude Code session itself acts as the extractor.
