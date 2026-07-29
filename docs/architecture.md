# PKB Architecture

PKB does **two things**.

1. **Claude Code searches and writes personal knowledge via MCP** (Elasticsearch hybrid search).
2. **Humans read markdown in Obsidian** — both the corpus originals (`data/`) and the concept
   notes projected from the SQLite concept graph (`data/_concepts/`) are plain markdown files, so
   they can be viewed as-is.

100% local — no external API calls, and no `ANTHROPIC_API_KEY` is needed. Concept extraction and
document writing are both performed by the Claude Code session itself. The one LLM call that does
exist in the code is optional and stays on the machine: `pkb graph rebuild-evidence-local` posts to
a local Ollama endpoint (`http://127.0.0.1:11434`, `graph/rebuild.py`) to automate the evidence
rebuild loop. Nothing else in the codebase talks to a generation model.

The core architecture has four layers:

1. **`data/`** — the personal document source of truth. Its location is set via `DATA_ROOT`, and
   placing it under an Obsidian vault (e.g. `<vault>/PKB`) is recommended.
2. **Elasticsearch** — chunk-level search index (nori BM25 + dense_vector kNN)
3. **SQLite Graph DB** — concept/relationship graph, one-way projected into `data/_concepts/`
4. **MCP server** — the primary interface Claude Code calls. The CLI is a secondary path for
   operations and verification.

---

## Overall Diagram

```
[User]
  ↓
[Claude Code]
  ↓ MCP (Streamable HTTP, 127.0.0.1:8787)
[PKB MCP Server]  src/pkb/mcp_server.py  (22 tools)
  ├─ search_knowledge        → ES hybrid search
  ├─ write_file / add_document / convert_and_ingest  → write/ingest into data/
  ├─ list_documents / get_document / reindex_document
  ├─ sync_corpus / sync_obsidian     → reconcile source↔ES (clean up ghost documents)
  ├─ archive_document / restore_document → soft delete/restore
  ├─ doctor                  → health check
  └─ graph_*(vocabulary/chunks/storage/curation/merge) / sync_concept_notes → concept graph

[Human]
  ↓ view directly via Obsidian
  data/ originals + data/_concepts/*.md projected notes
```

### Data Flow

```
[Ingest]  src/pkb/ingest.py
  Documents in data/ (md/pdf/docx/…)
    → parse frontmatter → chunk by heading hierarchy (+ merge tiny chunks)
    → compare content_hash delta → embed only changed chunks
    → apply to Elasticsearch via bulk (index/update/delete)

[Graph]  src/pkb/graph/
  ES chunks → graph_list_chunks → Claude Code self-extraction → graph_store_concepts
    → store in data/.graph/pkb_graph.sqlite → sync_concept_notes → project into data/_concepts/<slug>.md
```

Secondary interface: `src/pkb/cli.py` (`pkb init/reindex/sync/query/graph …`). Parity between the
CLI and MCP tools is enforced via introspection by `tests/test_cli_mcp_parity.py` — adding a new
command/tool without a mapping will be caught by this test.

---

## 1. Source Repository (`data/`)

The actual location is set via `DATA_ROOT` (default: `data/` inside the project). **Placing it as
a subfolder under an Obsidian vault is recommended** (e.g. `<vault>/PKB`) — since the corpus
itself lives inside the vault, it can be viewed, edited, and backlinked directly in Obsidian, and
`write_file`/`sync_concept_notes` output also shows up in the vault as-is. Regardless of location,
doc_id is always fixed as `data/<relative-path>`.

**The top-level folder name is the category, and it's dynamic** — the code doesn't enforce a fixed
list of categories, so creating a new folder creates a new category (`ingest.py:_extract_category`).

```
data/                       # = DATA_ROOT. doc_id is always data/… regardless of location
├── study/                  # folder = category. Subdirectory structure is free-form (e.g. rag/retrieval/, rag/rerank/…)
├── career/
├── writing/
├── about/
├── ...                     # adding a folder = adding a category (news, journal, agent, backend…)
├── _concepts/              # concept notes (one-way SQLite→note projection, not indexed in ES)
├── .logs/                  # search log JSONL (search_log.py)
└── .graph/                 # graph SQLite (GRAPH_DB_PATH)
```

These folder names are only an example — the code enforces no fixed list. How you split the corpus
is a policy choice: keep everything under `data/`, or keep topical material in `data/` and leave
personal folders elsewhere in the vault, where the optional `obsidian/` crawl described below picks
them up. Chunk/document counts vary with folder structure, so check `list_documents` for current
numbers.

Excluded from indexing (`ingest.py:EXCLUDED_DIR_NAMES`, `is_concept_path`):

| Target | Reason |
|---|---|
| `_review/`, `_trash/`, `_materials/`, `_archive/` | pending review, discarded, duplicate, or archived originals |
| `_origin/` | external source archive — only digested notes are indexed; originals are for evidence-checking only |
| any folder starting with `.` | tool output (`.obsidian`, etc.) |
| `data/_concepts/` | concept notes are a SQLite→note projection, so they must not be double-indexed in ES |

Supported formats:

| Extension | Handling |
|-------|------|
| `.md`, `.markdown`, `.txt` | read directly |
| `.pdf` | extracted with `pdfminer`, preserving page markers (`## p.N`) |
| `.docx`, `.pptx`, `.xlsx`, `.html` | converted to markdown with `markitdown` |

### Obsidian (`OBSIDIAN_PATH`) — Optional Path for Files Outside the Corpus

The default picture is "everything lives in the `data/` corpus." Since `data/` already sits inside
the vault, this mechanism is usually unnecessary. However, if you want to keep files outside
`DATA_ROOT` in the vault (personal folders like about, career, writing, exercise, or general notes
not moved into PKB) and still have them searchable, set `OBSIDIAN_PATH` in `.env` — **the category
is derived dynamically from the first folder name in the vault** (same rule as `data/`,
`ingest.py:_extract_category`), and it's crawled separately with `doc_id=obsidian/<vault-relative-path>`
(e.g. `obsidian/career/x.md` → `category=career`). The `DATA_ROOT` subtree is automatically
excluded, so there's no double ingestion. Original files are never copied or modified.

---

## 2. Elasticsearch Search Index

Runs as the Docker container `pkb-es`, with `pkb_documents` as the default index.

### Storage Unit

A single file is split into multiple chunks, and each chunk is stored as one ES document
(`_id = f"{doc_id}_{chunk_index}"`, e.g. `data/study/rag/example.md_0`) (`store.add_chunks`/
`apply_chunk_delta`).

```python
{
    "content": "Chunk text...",
    "embedding": [0.1, 0.2, ...],
    "source_path": "data/study/rag/example.md",
    "doc_id": "data/study/rag/example.md",
    "category": "study",
    "chunk_index": 0,
    "section_path": "Major Topic > Subtopic > Detail",
    "title": "Document Title",
    "tags": ["rag", "search"],
    "date_modified": "2026-04-16",
    "language": "ko",
    "content_hash": "sha256(content)",   // for delta embedding comparison
    # The lifecycle fields below exist only if they were set — by default they're not null,
    # the field simply isn't present at all
    # "expires_at": "...",     only if set in frontmatter
    # "archived_at": "...",    only when archive_document was called
    # "archive_reason": "...", only if an archive reason was given
}
```

### Chunking

- If YAML frontmatter exists, parse metadata such as `title`, `tags`, `expires_at`
- Store the H1–H3 heading path as `section_path` (derived from the file path if absent)
- Within a section, split by 500 tokens with 100-token overlap by default (`CHUNK_SIZE`/
  `CHUNK_OVERLAP`)
- Chunks under 80 characters (`MIN_CHUNK_CHARS`) are merged into an adjacent chunk — prevents
  low-information chunks containing only headings/links from dominating the top of rerank results
  (`ingest.py:_merge_tiny_chunks`)

### Delta Embedding

When a document is re-ingested, only changed chunks are re-embedded. Each chunk's `content_hash`
(SHA-256) is compared against the existing chunk at the same `chunk_index` (`ingest.py:ingest_files`).

| Comparison result | Action | Embedding cost |
|-----------|------|------------|
| hash identical | reuse | 0 |
| hash identical + only metadata differs | partial update | 0 |
| slot mismatch, same hash found in a different slot within the document | copy embedding (moved) | 0 |
| otherwise new/changed | re-embed + index | only the changed portion |
| slot no longer present in the new chunk set | delete | 0 |

`store.apply_chunk_delta` applies index/update/delete in a single bulk call.

### Search Pipeline

`search_knowledge` (MCP) and `pkb query` (CLI) share the same pipeline (`retrieve.hybrid_search`).

1. BM25 search — nori Korean analyzer, weighted matching across `content`/`title`/`section_path`
2. kNN search — sentence-transformers embeddings, ES dense_vector (HNSW)
3. RRF fusion — combines both candidate sets via Reciprocal Rank Fusion (`RRF_K=60`, fixed logic)
4. CrossEncoder reranking — disabled by default (`RERANK_ENABLED=false`), model
   `BAAI/bge-reranker-v2-m3`. In the 2026-07 benchmark, no-rerank (MRR 0.517) beat bge (0.388) and
   Qwen3-0.6B (0.492) on the BGE-M3 candidate pool, hence off by default.
5. **Cap of at most 2 chunks per document** (`MAX_CHUNKS_PER_DOC`) — ensures diversity so a single
   document can't dominate the top results
6. Optional: `EXPAND_CONTEXT=N` — attaches N chunks before/after each result as `neighbors`

The lifecycle filter (`retrieve._lifecycle_filter`) excludes chunks with an `archived_at` value
and chunks past their `expires_at` by default. `search_knowledge` parameters: `query`, `category`,
`top_k`, `include_archived`, `include_obsidian` (default True — if False, excludes documents with
an `obsidian/`-prefixed doc_id, i.e. vault documents outside the corpus).

Search calls are logged as JSONL to `data/.logs/search.jsonl`.

---

## 3. SQLite Concept Graph

Graph RAG doesn't replace search — it complements it by answering relationship queries between
concepts. It's stored at `data/.graph/pkb_graph.sqlite` (`GRAPH_DB_PATH`), with `concepts`,
`concept_aliases`, `documents`, `concept_edges`, `concept_edge_evidence`, `extracted_chunks`,
`concept_mentions`, `concept_curation`, and `graph_meta` as the nine tables (`graph/schema.py`). The weight/evidence_count on
`concept_edges` is aggregated from per-chunk `concept_edge_evidence`, so it stays accurate through
re-extraction and document deletion.

Build pipeline (entirely Claude Code self-extraction, no API calls):

1. Read chunks page by page with `graph_list_chunks(category|doc_id, offset, limit)`
2. Claude Code extracts concepts/relationships from the chunk content
3. Store into SQLite with `graph_store_concepts(items_json)` (reflects normalization, aliases,
   mentions, edges)
4. One-way project into `data/_concepts/<slug>.md` notes with `sync_concept_notes(confirm_prune)`
   (+ render the `_concepts/index.md` MOC)
5. Query SQLite directly with `graph_explain`, `graph_path`, `graph_query`, or `graph_affected`;
   each returned edge carries confidence and bounded source-chunk evidence
6. Read projected notes as the human-facing Obsidian view; use `search_knowledge` for source text

See [docs/graph-rag.md](graph-rag.md) for detailed design, normalization rules, and note rendering
format.

---

## 4. MCP Server

`src/pkb/mcp_server.py` is PKB's primary interface. It transports over Streamable HTTP, and a
single server kept always-on via launchd is shared by Claude Code, Codex, and Gemini.

```
Claude Code / Codex / Gemini → HTTP :8787/mcp → mcp_server.py ─┐
                                                               ├→ operations.py / documents.py
uv run pkb <command> ────────────────────────→ cli.py ─────────┘   (shared domain core)
                                                                          ↓
                                                                 ES / data / SQLite
```

`mcp_server.py` and `cli.py` are both thin surfaces. The write/convert/sync domain operations live
in `src/pkb/operations.py`, and document path resolution, lookup, and lifecycle live in
`src/pkb/documents.py` — extracted so that a tool and its CLI twin cannot drift apart. This shared
core is what actually makes the CLI↔MCP parity above true. `tests/test_cli_mcp_parity.py` guards
the *surface* by introspection — it fails when a newly registered command or tool isn't declared in
its capability map or an allowlist — but it never compares behavior; the shared core does that.

22 tools provided:

| Category | Tools |
|------|------|
| Search | `search_knowledge` |
| File/document | `write_file`, `list_documents`, `add_document`, `convert_and_ingest`, `get_document`, `reindex_document`, `sync_corpus`, `sync_obsidian` |
| Lifecycle | `archive_document`, `restore_document` |
| Status | `doctor` |
| Graph RAG | `graph_explain`, `graph_path`, `graph_query`, `graph_affected`, `graph_list_concepts`, `graph_list_chunks`, `graph_store_concepts`, `graph_curate`, `graph_merge`, `sync_concept_notes` |

See [docs/mcp.md](mcp.md) for each tool's parameters and usage examples.

Boundaries the MCP server enforces:

- `write_file` only writes `.md` files under `data/`
- `add_document` only ingests under `data/`
- `convert_and_ingest` can read external files, but the result is always saved under
  `data/<category>/`
- Obsidian sync (`sync_obsidian`) never modifies the original vault — it only updates ES

---

## 5. Secondary Interface (CLI)

`src/pkb/cli.py` is used for operations and verification. 19 top-level commands: `init`, `reindex`,
`index-switch`, `sync`, `convert`, `add`, `write`, `show`, `reindex-doc`, `list`, `query`, `delete`,
`archive`, `restore`, `doctor`, `eval`, `purge-archived`, `stale`, `watch`. Plus 10 `graph`
subcommands: `stats`, `explain`, `path`, `query`, `affected`, `map`, `reset-evidence`,
`finalize-evidence`, `rebuild-evidence-local`, `sync-notes`.

Most capabilities exist on both the CLI and MCP sides (`sync` ↔ `sync_corpus`, `show` ↔
`get_document`, `graph explain` ↔ `graph_explain`, etc.). CLI-only: `init`, `reindex`,
`index-switch` (read-alias switch), `delete`, `purge-archived`, `eval`, `graph stats`, `graph map`
(offline HTML snapshot), `graph reset-evidence`, `graph rebuild-evidence-local`,
`graph finalize-evidence` (heavy evidence migration), `stale`, `watch` (for hooks/daemons).
MCP-only: `graph_list_concepts`,
`graph_list_chunks`, `graph_store_concepts`, `graph_curate`, `graph_merge` (Claude self-extraction
loop), `sync_obsidian` (vault-only reconciliation). This mapping is guarded by
`tests/test_cli_mcp_parity.py`.

---

## 6. Document Lifecycle

```
New/modified ─ ingest_files (delta embedding)
Archived     ─ archived_at = now   → excluded from search, restorable (archive/restore_document)
Expired      ─ expires_at < now    → automatically excluded from search (set via frontmatter)
Deleted      ─ delete_document      → hard delete (irreversible) / purge-archived
```

`_lifecycle_filter` in `retrieve.py` filters out chunks with an `archived_at` value or a past
`expires_at` during default search. Setting `include_archived=True` disables the filter.

---

## 7. Sync Responsibility Matrix

| Situation | Action |
|---|---|
| Directly editing a single file (editor/Obsidian) | `pkb reindex-doc <doc_id>` / MCP `reindex_document` |
| Adding/moving/deleting many files (bulk changes) | `pkb sync` / MCP `sync_corpus` (+ `sync_obsidian` if needed) |
| Updating the concept graph with the latest content | Claude self-extraction (`graph_list_chunks`→`graph_store_concepts`), then `pkb graph sync-notes` / MCP `sync_concept_notes` |
| Mapping schema changed or index state got tangled | `pkb reindex --yes` (delete everything and rebuild) |

---

## 8. Operations

`docker-compose.yml` only spins up the `pkb-es` (Elasticsearch) container. The MCP server and CLI
run directly on the host via `uv run`, connecting to ES at `localhost:9200`.

```bash
docker compose up -d          # pkb-es (localhost:9200)
uv run pkb sync                # daily reconciliation (delta upsert + clean up ghost documents)
uv run pkb reindex --yes      # full reindex of data/ + Obsidian (when mapping changes)
```

---

## Real-World Flow Examples

### Example 1: Creating a Summary Note from Sources

```
User: "Summarize the saved content about BM25 and save it to data/writing/bm25.md"
  ↓
Claude Code
  ├─ search_knowledge(query="BM25", category="rag")
  ├─ read the search results and write a summary
  └─ write_file(file_path="data/writing/bm25.md", content="...")
       └─ automatically ingested after saving
```

### Example 2: Adding an External PDF

```
User: "Add ~/Downloads/paper.pdf under the study category"
  ↓
Claude Code
  └─ convert_and_ingest(input_path="~/Downloads/paper.pdf", category="study")
       ├─ convert with pdfminer, preserving pages (`## p.N` markers + provenance frontmatter)
       ├─ save to data/study/paper.md
       └─ ingest into ES
```

### Example 3: Querying Concept Relationships

```
User: "How are DI, IoC, Bean, and Container connected?"
  ↓
Claude Code
  ├─ graph_query(query="How are DI, IoC, Bean, and Container connected?")
  ├─ inspects confidence plus doc_id/chunk_index evidence on returned edges
  └─ uses search_knowledge/get_document when source text is needed
```

---

## Why This Design

| Component | Role | Why chosen |
|---------|------|-----------|
| MCP | Primary interface | Lets Claude Code call personal knowledge tools directly |
| `data/` (inside vault) | Source repository | A single source of truth a human can read and edit in Obsidian |
| Elasticsearch | Search index | Runs Korean keyword search and vector search together |
| RRF + reranker | Search quality | Combines the strengths of keyword/semantic search and refines final precision |
| SQLite Graph DB | Concept relationship storage | Low install/ops overhead and easy backup at personal scale |
| CLI | Secondary interface | Well-suited for reindexing, debugging, and checking search quality |

## Swappable Components

- **Elasticsearch → Qdrant/Chroma/pgvector**: replace `store.py`, `retrieve.py`
- **sentence-transformers → a different embedding model**: replace `embeddings.py`
- **SQLite Graph DB → Neo4j**: replace the `src/pkb/graph/store.py` layer
- **markitdown → docling/unstructured**: replace the file-reading path in `ingest.py`
- **Claude Code MCP → a different MCP client**: `mcp_server.py` can be used as-is
