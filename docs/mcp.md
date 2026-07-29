# MCP Integration (Basic Usage)

PKB's primary interface is **agent + MCP**. Claude Code, Codex, and Gemini CLI act as the agent, while PKB exposes search, file-writing, ingestion, and graph-query tools via MCP.

The CLI is a secondary interface for indexing, validation, and debugging. Day-to-day use happens through agent conversations.

## Why a Shared HTTP Server Instead of stdio

> **Do not register PKB via stdio.** MCP stdio spawns **a new server process for every session.**
> The PKB server loads an embedding model and a CrossEncoder reranker, so a single process uses
> roughly **4GB**. Open several sessions or fan out across agents, and memory usage becomes
> session count × 4GB, swapping the machine to death.
>
> That's why PKB runs as **a single HTTP server bound to 127.0.0.1**, shared by all clients. Only
> one copy of the models stays resident, so memory stays flat no matter how many sessions you open.

Streamable HTTP is a transport designed for multiple clients to share a single server process, so this setup aligns with the spec.

## Prerequisites

- Start Elasticsearch: `docker compose up -d`
- Install Python dependencies: `uv sync`
- Initialize the index: `uv run pkb init`

No API key is required — the agent acts as the LLM, so the entire setup runs 100% locally.

## 1. Keeping the Server Running (macOS / launchd)

`~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.jongkwan.pkb-mcp</string>
    <key>ProgramArguments</key>
    <array>
        <string>/ABSOLUTE/PATH/TO/uv</string>
        <string>--directory</string>
        <string>/ABSOLUTE/PATH/TO/personal-docs</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>pkb.mcp_server</string>
    </array>
    <!-- config.py's env_file=".env" is a relative path, so cwd must be the repo root -->
    <key>WorkingDirectory</key>
    <string>/ABSOLUTE/PATH/TO/personal-docs</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/pkb-mcp.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/pkb-mcp.err.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist
```

It starts automatically at login, and `KeepAlive` restarts it if it dies. Because of the
`RunAtLoad`+`KeepAlive` combination, **`pkill` won't stop it** — use `launchctl unload` instead.

You can also run the server directly (for development/debugging). Just **don't run two instances
at once** — a port conflict sends launchd into an infinite restart loop, and each failed startup
attempt eats memory trying to load the models.

```bash
uv run python -m pkb.mcp_server   # foreground
```

## 2. Registering Clients

All three clients support streamable HTTP. **The argument syntax differs between them.**

```bash
# Claude Code
claude mcp add --transport http pkb http://127.0.0.1:8787/mcp -s user

# Codex  (--url flag)
codex mcp add pkb --url http://127.0.0.1:8787/mcp

# Gemini CLI  (-t http, and -s user is required — default scope is project, so omitting it registers the server only for that folder)
gemini mcp add pkb http://127.0.0.1:8787/mcp -t http -s user
```

Gemini **suppresses MCP in untrusted folders** — even user-scoped servers get blocked. If your
working folder isn't listed in `~/.gemini/trustedFolders.json`, it shows up as `Disabled`.

### Claude Desktop (Requires a Bridge)

Claude Desktop is the only client that can't connect to HTTP directly. The `mcpServers` section in
`claude_desktop_config.json` is **stdio only**, and custom connectors under Settings > Connectors
route through Anthropic's cloud to reach the server, so they can't reach localhost (plain
`http://` is rejected even for localhost).

> ⚠️ Do not add a `"url"` field to `claude_desktop_config.json`. Desktop will **silently drop the
> entire `mcpServers` section without any error.**

That's why we insert a stdio↔HTTP bridge (`mcp-remote`). `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pkb": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8787/mcp", "--allow-http"]
    }
  }
}
```

The bridge is a lightweight Node process and doesn't load any models — the heavy 4GB footprint
belongs to the single shared HTTP server. Restart Desktop after configuring this.

To change the port, update `MCP_PORT` in `.env` and update the URLs above accordingly.

## 3. Verification

```bash
uv run pkb doctor
```

The `=== MCP 서버 ===` section shows LISTEN status, PID, uptime, memory, device, and the launchd
cumulative start count. If the cumulative start count is greater than 1, the server has been
restarting repeatedly — check `pkb-mcp.err.log`.

For each client, verify the connection with `claude` → `/mcp`, `codex mcp list`, and
`gemini mcp list`.

## Available Tools

### Search / Document Operations

| Tool | Description |
|------|------|
| `search_knowledge` | Hybrid search over the personal knowledge base. Runs BM25 and kNN in a single msearch request and combines them with RRF (CrossEncoder reranking is disabled by default), capped at 2 chunks per document. When a concept graph exists, each hit is annotated with related concept links and a concept vocabulary for follow-up searches. Pass `include_obsidian=False` to exclude documents outside the corpus (vault-only, `obsidian/`-prefixed doc_id). If the default search comes up thin, add `query_variants` (up to 3 RAG-Fusion query rewrites) to search the variants together and merge results with RRF |
| `write_file` | Writes `.md` files under `data/`. By default, auto-ingests immediately after writing |
| `list_documents` | Lists documents stored in ES. Shows the top `limit` (default 50) sorted by `date_modified` descending. Set `limit<=0` to list everything |
| `add_document` | Ingests a file under `data/` (md/txt/pdf/docx/pptx/xlsx/html) |
| `convert_and_ingest` | Converts an external PDF/DOCX/PPTX/XLSX/HTML file to `.md`, saves it under `data/<category>/`, and ingests it. The converted file's frontmatter records provenance (`source`, `converted_from`, `converted_at`); PDFs keep page markers (`## p.N`) |
| `sync_corpus` | Reconciles the `data/` corpus with ES: upserts plus pruning of documents no longer in the corpus (`confirm_prune=True` approves bulk deletions) |
| `sync_obsidian` | Reconciles the Obsidian vault with ES: upserts plus pruning of documents no longer in the vault. Categories are derived dynamically from the vault's top-level folder name (same rule as `data/`). If `OBSIDIAN_PATH` isn't set, it proposes pruning all remaining `obsidian/*` documents (`confirm_prune=True` required) |
| `get_document` | Fetches a document. By default (`include_content=False`), returns only metadata (title, category, modified date) plus a chunk table of contents. Use `chunk_range` (e.g. `"3-7"`) to get the full text of that range, or `include_content=True` for the full text |
| `reindex_document` | Re-reads a specific source document and re-ingests it into ES |
| `archive_document` | Archives a document as a soft delete (excluded from search, recoverable). For `.md` originals under `data/`, records `archived_at` in the frontmatter and re-ingests (the file is the SSOT); for everything else, the record is written only to ES |
| `restore_document` | Restores an archived document so it's searchable again. If the frontmatter has archive metadata, it's removed before re-ingesting |
| `doctor` | Checks ES connectivity, indices, chunk counts, configuration, and concept graph stats, and lists candidate actions (expired-but-not-archived documents, purge candidates, orphan concepts) |

### Graph RAG

| Tool | Description |
|------|------|
| `graph_list_concepts` | Looks up the concept vocabulary already in the graph — call this before extraction to reuse existing name/slug values for overlapping concepts |
| `graph_explain` | Returns one concept with aliases, curation, inbound/outbound relations, mentions, confidence, and source-chunk evidence |
| `graph_path` | Finds a bounded shortest path between two concepts; supports directed traversal and relation filters |
| `graph_query` | Uses the local concept embedding model to select semantic/lexical seeds, then returns a bounded SQLite relationship subgraph |
| `graph_affected` | Traverses stored `src→dst` direction from one concept to show downstream concepts, optionally filtered by relation type |
| `graph_list_chunks` | Lists chunks for concept extraction, paginated. With `pending_only=True`, returns only chunks that haven't been extracted yet or whose content changed (incremental extraction) |
| `graph_store_concepts` | Stores the concept/relation JSON extracted by Claude Code into the SQLite graph DB |
| `graph_curate` | Concept curation — lists uncurated concepts, saves real/vocab labels and prose |
| `graph_merge` | Merges notation-variant concepts into a canonical concept (carrying over edges, mentions, and aliases) |
| `sync_concept_notes` | Projects the SQLite concept graph into `data/_concepts/<slug>.md` vault notes and renders `_concepts/index.md` as a MOC (the concept vocabulary catalog entry point) |

Graph RAG's MCP-first workflow: read chunks with `graph_list_chunks`, have Claude Code extract concepts and relations directly, and store them with `graph_store_concepts`. After storing, label real/vocab with `graph_curate`, merge notation variants with `graph_merge`, and project the result with `sync_concept_notes`. Query SQLite directly with `graph_explain`/`graph_path`/`graph_query`/`graph_affected`; use projected notes as the human-readable Obsidian view.

### Parameter Reference

Exact signatures as registered in `src/pkb/mcp_server.py`:

```python
search_knowledge(query, category="", top_k=5, include_archived=False,
                 include_obsidian=True, query_variants=[])
write_file(file_path, content, ingest=True)
list_documents(category="", include_archived=False, limit=50)
add_document(file_path, tags="")
convert_and_ingest(input_path, category, output_name="", ingest=True)
get_document(doc_id, include_content=False, chunk_range="")
reindex_document(doc_id)
sync_corpus(confirm_prune=False)
sync_obsidian(path="", confirm_prune=False)
archive_document(doc_id, reason="")
restore_document(doc_id)
doctor()

graph_list_concepts(category="", limit=500)
graph_explain(concept, edge_limit=30, evidence_limit=5, mention_limit=20)
graph_path(source, target, max_hops=4, directed=False, relations=[], evidence_limit=3)
graph_query(query, depth=2, seed_limit=3, max_nodes=30, min_similarity=0.4,
            relations=[], evidence_limit=3)
graph_affected(concept, max_depth=2, max_nodes=30, relations=[], evidence_limit=3)
graph_list_chunks(category="", doc_id="", offset=0, limit=20, pending_only=False)
graph_store_concepts(items_json)
graph_curate(items_json="")
graph_merge(winner_slug, loser_slugs_json)
sync_concept_notes(confirm_prune=False)
```

`add_document(tags=...)` takes a comma-separated string, while the graph tools take
`relations` as a **list**. The CLI equivalents diverge here on purpose: `pkb graph …` uses
`--relation` with a comma-separated string (`cli.py:_graph_relations`), because shells handle a
single string better than a repeated flag.

## Usage Examples

Having a natural conversation in Claude Code triggers the appropriate MCP tool calls.

### Search

- *"Find Python-related projects in my career history"* → `search_knowledge(category="career")`
- *"Is there anything about vector search in my rag notes?"* → `search_knowledge(category="rag")`
- *"Find notes about DI, IoC, dependency injection"* → `search_knowledge(query="DI IoC dependency injection")`

### Adding External Files

- *"Add this PDF paper to the study category: `~/Downloads/paper.pdf`"* → `convert_and_ingest(category="study")`
- *"Convert `~/Documents/notes.docx` and add it to the writing category"* → `convert_and_ingest(category="writing")`
- *"Bring the data corpus up to date"* → `sync_corpus()` (upsert + prune stale documents)
- *"Also sync the vault files left outside the corpus"* → `sync_obsidian()` (uses OBSIDIAN_PATH from .env)

`category` is required for `convert_and_ingest`. Claude Code infers it from the filename, user instructions, or content context. Category = the corpus's top-level folder name (dynamic); existing categories are reused when possible, and a new name creates a new category folder. You can also specify a subfolder path like `study/payments` (the category is the first folder).

### Document Management

- *"Show me the list of stored career documents"* → `list_documents(category="career")`
- *"Just show me the table of contents for this document: `data/study/rag/xxx.md`"* → `get_document(doc_id="data/study/rag/xxx.md")` (default is metadata + TOC)
- *"Show me the full content of this document"* → `get_document(doc_id="...", include_content=True)`
- *"Re-index the document I just edited"* → `reindex_document(doc_id="data/study/rag/xxx.md")`

### File Creation

- *"Summarize what we just searched and save it to `data/writing/summary.md`"* → `search_knowledge` + `write_file`
- *"Create a summary note based on what you found"* → search → draft → `write_file` → auto-ingest

### Concept Graph

- *"Build the concept graph for the rag material, 20 chunks at a time"* → repeated `graph_list_chunks(category="rag", limit=20)` + `graph_store_concepts`
- *"Sync the concept notes to the vault"* → `sync_concept_notes()`
- *"Explain Dependency Injection and show its evidence"* → `graph_explain(concept="Dependency Injection")`
- *"What is the shortest relationship path from DI to Container?"* → `graph_path(source="DI", target="Container")`
- *"How are DI, IoC, Bean, and Container connected?"* → `graph_query(query="How are DI, IoC, Bean, and Container connected?")`
- *"What depends on DI?"* → `graph_affected(concept="DI", relations=["prerequisite_of"])`

## Flow

```
Claude Code conversation
    ↓
Claude Code judges user intent and what evidence is needed
    ↓
Calls PKB MCP tools
    ├─ search_knowledge      → ES hybrid search
    ├─ write_file            → writes .md under data/ + auto-ingest
    ├─ add_document          → chunking → embedding → ES indexing
    ├─ convert_and_ingest    → convert to md (pdfminer preserves pages for PDF, markitdown for others) → save to data/ → ES indexing
    ├─ sync_corpus / sync_obsidian → reconcile with source (upsert + prune stale documents)
    ├─ get_document          → look up chunks/section_path per document
    └─ graph_*               → query/store the SQLite concept graph
    ↓
Tool results feed back into the Claude Code conversation context
```

Within MCP, Claude Code itself handles tool selection, re-search, summarization, and file writing.

## Notes

- The data corpus's actual location is determined by `DATA_ROOT` (default: `data/` inside the project). Regardless of that location, tool input paths and doc_id always use the `data/...` format.
- `write_file` and `add_document` only accept paths under the corpus (`data/...`).
- `write_file` only accepts `.md` files and, by default, ingests immediately after saving.
- `convert_and_ingest` places no restriction on where the source file lives, but the converted output is always saved under the corpus's `<category>/`.
- `sync_corpus` is the default reconciliation tool (upserts the entire `data/` corpus + prunes stale documents). `sync_obsidian` is an optional path used only when vault files are left outside `DATA_ROOT`: it reads the external Obsidian vault and writes only to ES (the source files are never copied or modified). If `DATA_ROOT` sits inside the vault, that subtree is excluded from the crawl (to avoid double ingestion). Both tools prune documents that have disappeared from the source, but bulk deletions of 21+ documents require `confirm_prune=True`.
- Large-scale Graph RAG construction can take a while, so start with a specific category (e.g. `rag`) or a single `doc_id`.
