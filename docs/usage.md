# CLI Usage (Alternative Interface)

> PKB's **primary usage path is [MCP integration](mcp.md)**. This document covers the alternative path — indexing, searching, and maintaining PKB directly via the CLI without Claude Code.

## Prerequisites

No API key is needed. Use `.env` only when a local path setting like `OBSIDIAN_PATH` is required:

```bash
cp .env.example .env
```

---

## Document Ingestion

Supported formats: `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`

```bash
# Single file
uv run pkb add data/about/bio.md
uv run pkb add data/study/paper.pdf

# Whole directory (recursive, auto-filters to supported formats)
uv run pkb add data/study/

# With tags
uv run pkb add data/study/ --tags "python,backend,fastapi"
```

`data/` directory structure — **the top-level folder name is the category, and it's dynamic** (add a folder = add a category):

```
data/
├── study/       # Study notes, papers
├── career/      # Career, tech stack, projects
├── writing/     # Drafts, notes
├── about/       # Bio, interests
└── ...          # Add a folder = add a category (e.g. news, journal, exercise)
```

Excluded from indexing: `_review/`, `_trash/`, `_materials/`, `_archive/` (pending review, discarded,
duplicates, archived originals), `_origin/` (external source archive — only the digested notes are
indexed, the originals are for provenance checks only), any folder starting with `.` (tool
artifacts), and the retired `_concepts/` path if a legacy backup is restored.

---

## Writing Rules

Rules Claude Code follows before saving a new document with `write_file`.

- Polish the content before saving — readable prose (Zettelkasten tone), with frontmatter (title, tags, `expires_at` if needed).
- Placement decisions: (1) prefer an existing topic folder (check with `list_documents`) (2) one-off
  research goes to `<topic>/research/` under the matching topic with curated frontmatter (3) only
  create a new folder for a planned series (4) promote to a folder once 2-3 items on the same topic
  pile up. Dates go in frontmatter, not the filename. `daily-research/` is a legacy inbox: new
  documents are rejected in strict mode; editing existing files is still allowed.

---

## Converting External Files (PDF/DOCX/PPTX/XLSX/HTML → .md)

The original file can live anywhere. The converted `.md` is saved to `data/<category>/` and
auto-ingested. The converted file gets provenance frontmatter (`source`, `converted_from`,
`converted_at`) at the top, and PDF page markers (`## p.N`) are preserved:

```bash
# Default: saved under the misc category
uv run pkb convert ~/Downloads/paper.pdf
# → saved to data/misc/paper.md

# Explicit category (recommended)
uv run pkb convert ~/Downloads/paper.pdf --category study

# Skip auto-ingest (review first, ingest manually)
uv run pkb convert ~/Downloads/doc.docx --no-ingest --category study
uv run pkb add data/study/doc.md   # ingest later
```

On the default MCP path, Claude Code sets `category` directly when calling `convert_and_ingest`.

---

## Search

```bash
uv run pkb query "how does vector search work?"
uv run pkb query "Python framework experience" --category career --top-k 10
uv run pkb query "RAG evaluation" --expand 1     # attach ±1 neighboring chunks
uv run pkb query "BM25" --rerank                 # force CrossEncoder reranking on for this run
uv run pkb query "BM25" --no-obsidian            # corpus only, drop obsidian/* documents
```

`--top-k` defaults to `DEFAULT_TOP_K` (5); `--rerank`/`--no-rerank` and `--expand` fall back to
`RERANK_ENABLED` and `EXPAND_CONTEXT` when omitted. **Note the asymmetric negative form** — the flag
pair is `--include-obsidian` / `--no-obsidian`, not `--no-include-obsidian`.

## Document Management

```bash
uv run pkb list
uv run pkb list --category study
uv run pkb list --limit 0                        # 0 = no limit (default 50)

uv run pkb show data/study/rag-overview.md                     # metadata + table of contents
uv run pkb show data/study/rag-overview.md --content           # include chunk bodies
uv run pkb show data/study/rag-overview.md --content --chunks "3-7"   # only chunks 3-7

uv run pkb write data/writing/note.md --content "..."   # write + auto-ingest (stdin if --content omitted)
uv run pkb write data/writing/note.md --no-ingest       # save only, ingest later

uv run pkb reindex-doc data/study/rag-overview.md       # re-ingest one document after an external edit
uv run pkb delete data/study/rag-overview.md
```

`write`, `show`, and `reindex-doc` are the CLI twins of `write_file`, `get_document`, and
`reindex_document` — same core path, same `data/`-only boundary.

### Watching for Changes

```bash
uv run pkb watch                 # poll every 10s
uv run pkb watch --interval 60   # poll every 60s
```

Runs one `sync`-equivalent reconcile at startup to establish a baseline, then delta-ingests only
changed files (mtime + size snapshot). Ctrl+C to stop.

---

## Rebuilding the Index

When the mapping schema has changed (e.g. adding a `section_path` field) or the index gets corrupted:

```bash
uv run pkb reindex           # asks for confirmation
uv run pkb reindex --yes     # run immediately
```

Fully re-indexes the data corpus (DATA_ROOT) and OBSIDIAN_PATH.

### Zero-Downtime Switch (`index-switch`)

`reindex` deletes and rebuilds in place, so search is degraded while it runs. When you change
`EMBEDDING_MODEL`/`EMBEDDING_DIMS` — which forces a full re-embed — build a new **physical** index
first, then flip the read alias atomically:

```bash
# 1. Fill a new physical index by overriding ES_INDEX for this run only
ES_INDEX=pkb_documents_v2 uv run pkb reindex --yes

# 2. Atomically repoint the read alias; prints the previous target
uv run pkb index-switch pkb_documents_v2
```

`ES_INDEX` is treated as a **read alias**, not a physical index. `index-switch` fails loudly if the
target index doesn't exist, if it equals the alias name, or if `ES_INDEX` is currently a physical
index rather than an alias (`store.switch_read_alias`). The old physical index is never deleted —
verify the new one, then remove it yourself.

---

## Sync (Reconciliation)

Reconciles ES with the sources of truth (data corpus + Obsidian vault) — in addition to upserting,
it **cleans up ghost documents that no longer exist in the source**:

```bash
uv run pkb sync          # asks for confirmation on large deletes (21+)
uv run pkb sync --yes    # skip confirmation
```

Configuration defines the expected state. Behavior by case:

| Situation | Behavior |
|---|---|
| No OBSIDIAN_PATH (from the start) | Obsidian side is a no-op |
| Already configured | Delta upsert + clean up files removed from the vault |
| Newly configured | Full vault ingest |
| Was configured, now removed | Offers to delete all remaining `obsidian/*` (requires confirmation) |
| Vault path changed | Ingest the new vault + clean up old documents |

Unlike `reindex` (full rebuild), `sync` only processes deltas, so it's meant for day-to-day
maintenance. `doctor` warns about config-index mismatches (e.g. integration turned off but documents remain).

### Stale Nudge (SessionStart Hook)

When `sync` succeeds, it records a timestamp in `data/.logs/last_sync.json`, and `pkb stale` reports
how many source files have changed since then (`--quiet` only prints output when stale). The hook
always exits 0 so it never breaks a session. Since it's an mtime scan, it doesn't detect deletions —
cleaning up deletions is `sync`'s prune job.

Register it as a Claude Code `SessionStart` hook to get notified whether a sync is needed at session
start (`~/.claude/settings.json`, replace `<repo-path>` with this repo's absolute path):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --project <repo-path> pkb stale --quiet"
          }
        ]
      }
    ]
  }
}
```

---

## Graph RAG Operations CLI

Building the concept graph is done directly by Claude Code via MCP's `graph_list_chunks`/`graph_store_concepts` (no API calls). The CLI covers stats, the four read queries, the Evidence Map snapshot, and evidence rebuild.

```bash
# Current graph stats
uv run pkb graph stats

# Read queries — same models the MCP graph_* tools use
uv run pkb graph explain "BM25"                 # one concept, inbound/outbound edges + evidence
uv run pkb graph path "BM25" "RRF"              # bounded shortest path
uv run pkb graph query "how do lexical and vector retrieval connect?"   # semantic-seeded subgraph
uv run pkb graph affected "BM25" --relation prerequisite_of  # stored-direction downstream

# Offline Evidence Map HTML (see docs/graph-rag.md for the full option table)
uv run pkb graph map --concept "BM25" --open

# Only run this to fully rebuild the old append-only relations onto an evidence basis
# Keeps the existing graph, resets only staging evidence and extraction markers
uv run pkb graph reset-evidence --yes

# Optional: auto-extract all pending items with an installed Ollama model (resumable if interrupted)
uv run pkb graph rebuild-evidence-local --yes
#   --model gpt-oss:20b            # any installed Ollama generation model
#   --batch-size 8                 # chunks per generation (1-8)
#   --max-batches 0                # 0 = run to completion, 1 = sample validation
#   --endpoint http://127.0.0.1:11434   # local Ollama endpoint

# After manually processing all graph_list_chunks → graph_store_concepts, atomically switch over once pending=0
uv run pkb graph finalize-evidence --yes
```

---

## Obsidian Integration

**Recommended setup**: point `DATA_ROOT` at a subfolder of your vault (e.g. `<vault>/PKB`). Since
the corpus source lives inside the vault, you can view and edit it directly in Obsidian, and the
doc_id stays `data/...` regardless of location. In this setup, the `OBSIDIAN_PATH` crawl below is
usually unnecessary.

**`OBSIDIAN_PATH` (optional)**: set this only if you want to also search vault files kept outside
`DATA_ROOT`. Setting `OBSIDIAN_PATH=/absolute/path/to/vault` in `.env`:

```bash
uv run pkb init    # creates the ES index + (if configured) initial Obsidian ingest
```

After that, reconcile vault changes with `uv run pkb sync` (or MCP's `sync_obsidian`). There's no
live watcher. Files ingested through this path get their `doc_id` stored as
`obsidian/<relative-path>`, and the **category is derived dynamically from the first folder name in
the vault** — the same rule as `data/` (`ingest.py:_extract_category`), so `obsidian/career/x.md`
becomes `category=career`, not `category=obsidian`. Files sitting directly at the vault root fall
back to `misc`. The `DATA_ROOT` subtree is automatically excluded from the crawl to avoid
double-ingestion.

---

## Project Structure

```
├── docker-compose.yml       # ES + nori container
├── Dockerfile.es            # ES image + nori plugin
├── pyproject.toml
├── .env.example             # Environment variable template
│
├── data/                    # Personal documents (gitignored, location configurable via DATA_ROOT)
│
└── src/pkb/
    ├── mcp_server.py        # MCP server (primary usage path)
    ├── cli.py               # CLI commands
    ├── operations.py        # Write/convert/sync domain core shared by CLI and MCP
    ├── documents.py         # Document path resolution, lookup, lifecycle
    ├── config.py            # Settings management
    ├── ingest.py            # Parsing, chunking (PDF preserves pages via pdfminer, others via markitdown)
    ├── embeddings.py        # sentence-transformers embeddings
    ├── rerank.py            # CrossEncoder reranking
    ├── store.py             # Elasticsearch CRUD, index management
    ├── retrieve.py          # Hybrid search (BM25 + kNN + RRF)
    ├── report.py            # doctor status report
    ├── eval.py              # Search quality evaluation (recall/MRR by mode)
    ├── search_log.py        # Search call JSONL logging
    └── graph/               # SQLite Graph RAG (schema, store, query, notes, rebuild, viewmap)
```

---

## Configuration

`.env` file:

```
ES_HOST=http://localhost:9200   # Elasticsearch host (default)
ES_INDEX=pkb_documents          # Index name (default)
OBSIDIAN_PATH=                  # (Optional) Absolute path to Obsidian vault
GRAPH_DB_PATH=data/.graph/pkb_graph.sqlite
GRAPH_DEDUP_THRESHOLD=0.88
```

Other tuning options in `pkb.config.Settings` (overridable via environment variables):

| Name | Default | Description |
|------|--------|------|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | sentence-transformers model (1024d, change together with `EMBEDDING_DIMS`) |
| `EMBED_CONTEXT_PREFIX` | `true` | Include title/section_path prefix in embedding input |
| `EMBEDDING_DEVICE` | `auto` | Torch device for embedding. `auto` picks mps → cuda → cpu; or pin `cpu`/`mps`/`cuda` |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder reranker model |
| `RERANK_DEVICE` | `auto` | Torch device for the reranker (same resolution rule) |
| `RERANK_BATCH_SIZE` | `8` | CrossEncoder batch size. Small batches measured faster on MPS |
| `RERANK_ENABLED` | `false` | Whether reranking is on by default |
| `CANDIDATE_K` | `20` | Number of RRF/reranker candidates. The default was chosen from rerank-path benchmarks, where `ck=20` gave 2.4x lower latency than `ck=50` with the same quality. In the same benchmark, the RRF-only path also showed a slight nDCG/MRR edge at `ck=20`. Raise to 50 if you need a larger candidate pool. |
| `EXPAND_CONTEXT` | `0` | If N>0, attaches N chunks before/after each search result as neighbors (parent context) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 100 | Fixed-size chunking |
| `GRAPH_DB_PATH` | `data/.graph/pkb_graph.sqlite` | SQLite concept graph file |
| `GRAPH_DEDUP_THRESHOLD` | `0.88` | Embedding similarity threshold for concept merging |
| `DEFAULT_TOP_K` | `5` | Default result count for `pkb query` and `search_knowledge` |
| `MCP_PORT` | `8787` | Shared HTTP MCP server port (always bound to `127.0.0.1`) |
| `WARMUP_ON_START` | `false` | Preload the models when the MCP server starts. Left off, the server idles at ~50MB until the first search |

## Chunking Strategy

- PDFs are extracted with `pdfminer`, preserving page markers (`## p.N`); other non-markdown formats (docx/pptx/xlsx/html) are converted to markdown with `markitdown`
- YAML frontmatter (if present) is parsed to extract `title`/`tags`/`expires_at`
- Split hierarchically at H1-H3 heading boundaries (each chunk records `section_path`: `topic > subtopic > detail`)
- Within a section, fixed-size 500 tokens with 100-token overlap
- Respects paragraph (`\n\n`) boundaries
- Micro-chunks under 80 characters (`MIN_CHUNK_CHARS`) are merged into adjacent chunks — this
  prevents low-information chunks (headings/links only) from dominating the top of the rerank

## Document Lifecycle (Expiry + Soft Delete)

You can automatically exclude stale material from search, or hide it in a recoverable way for later.

### 1. Set an Expiry Date (frontmatter)

```yaml
---
title: 2025 Resume
expires_at: 2026-01-01       # ISO date. Once past, automatically excluded from search.
tags: [resume, 2025]
---
```

`expires_at` accepts `date`/`datetime`/ISO string values. Invalid values are logged as a warning and ignored. Once the given date is before `now`, the document is automatically excluded from `hybrid_search` results (ES lazy filter, no batch job).

### 2. Manual Archive (soft delete)

```bash
# Hide an individual document (excluded from search, recoverable)
uv run pkb archive data/career/old_resume.md --reason "newer version exists"

# Restore
uv run pkb restore data/career/old_resume.md

# Permanently delete archived items (irreversible! only on explicit request)
uv run pkb purge-archived --before 2024-01-01
```

Also available as MCP tools:
- `archive_document(doc_id, reason="")` — from a Claude Code conversation, *"archive this document"*. For `.md` originals under `data/`, records `archived_at` in frontmatter and re-ingests so the status survives a reindex. The CLI `archive` command uses the same core path.
- `restore_document(doc_id)` — restore (removes the frontmatter record if present)

### 3. Filter Override — Include Archived

Calling `search_knowledge(query, include_archived=True)` / `list_documents(include_archived=True)` includes archived/expired documents too. The default is always to **exclude** them.

### 4. Check Status with doctor

```
Index 'pkb_documents': 13,033 chunks   # example (actual figure as of 2026-07)
  - study: 10598
  - career: 1871
  - writing: 222
  - news: 214
  - about: 81
  - exercise: 30
  - journal: 17
  archived: <N>  expired(still-visible): <N>
```

- `archived`: number of manually archived chunks (has `archived_at`)
- `expired(still-visible)`: past `expires_at` but not yet archived — excluded from search but still in the DB. Clean up with `pkb purge-archived`.
- Action candidates are listed by doc_id — expired-but-not-archived documents, purge candidates (archived 30+ days ago), orphan concepts (0 mentions). Judgment and resolution are left to the Claude session.

## Search Logs

Every search call is logged to `data/.logs/search.jsonl` (query, category, fusion, reranked, results).

```bash
tail data/.logs/search.jsonl | jq .
```

## Search Quality Evaluation (eval)

Runs a gold set (question → correct document) through 4 search modes (bm25-only / knn-only / rrf /
rrf+rerank) and compares recall@1/3/5/10 and MRR per mode. If the gold document falls outside the
top 10 (a miss), the actual #1 doc_id is reported alongside it.

```bash
uv run pkb eval                            # default: <DATA_ROOT>/.eval/gold.jsonl
uv run pkb eval --gold path/to/gold.jsonl
```

### Gold Set Authoring Guidelines

`data/.eval/gold.jsonl` — one entry per line:

```jsonl
{"query": "What scoring formula combines term frequency and document length in an inverted index?", "doc_id": "data/study/rag/1.2.10_BM25.md"}
```

- Have a Claude Code session read the target document directly and write the question. **No title
  words allowed** — reusing words from the filename or title lets BM25 match for free and destroys
  discriminative power. The question needs to paraphrase the content so differences between modes
  show up.
- `data/.eval/` is automatically covered by existing rules without extra config: folders starting
  with `.` are excluded from ingestion, so the gold set never mixes into the search corpus, and
  `data/` is gitignored so it's never committed.
```
