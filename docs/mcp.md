# MCP Integration (Basic Usage)

PKB's primary interface is **agent + MCP**. Claude Code, Codex, and Gemini CLI act as the agent, while PKB exposes search, file-writing, ingestion, and graph-query tools via MCP.

The CLI is a secondary interface for indexing, validation, and debugging. Day-to-day use happens through agent conversations. In this guide, an **agent/CLI session** means a Claude Code, Codex, or Gemini conversation/process; it is not an MCP protocol session.

## Why a Shared HTTP Server Instead of stdio

> **Do not register PKB via stdio.** MCP stdio spawns **a new server process for every agent/CLI session.**
> The PKB server loads an embedding model and a CrossEncoder reranker, so a single process uses
> roughly **4GB**. Open several agent/CLI sessions or fan out across agents, and memory usage becomes
> agent/CLI session count × 4GB, swapping the machine to death.
>
> That's why PKB runs as **a single HTTP server bound to 127.0.0.1**, shared by all clients. Only
> one copy of the models stays resident, so memory stays flat no matter how many agent/CLI sessions you open.

Streamable HTTP is a transport designed for multiple clients to share a single server process, so this setup aligns with the spec.

## Stateless Streamable HTTP (MCP 2026-07-28)

The 2026-07-28 MCP revision removes protocol sessions: there is no `initialize` handshake and no
`Mcp-Session-Id`. Each Streamable HTTP request is independent and carries its own metadata. Clients
automatically handle the `Mcp-Method` and `Mcp-Name` headers, so users do not need to add or maintain
headers manually. The endpoint and client-registration commands below remain unchanged.

Stateless transport does not mean a new PKB process per request. PKB still keeps one localhost HTTP
process (and one in-memory model set) shared by all clients. Cross-call application state lives in
Elasticsearch/SQLite or is passed explicitly as tool arguments/handles, never in a protocol session.

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
    <!-- launchd's default soft limit is 256 fds. torch + MPS alone keep ~220 dylibs and
         Metal shader caches open, so a long-lived server hits EMFILE and then fails to
         open corpus files or the graph SQLite. -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>4096</integer>
    </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist
```

Without the `NumberOfFiles` limit, fd exhaustion shows up as two unrelated-looking errors:
`write_file` reports `기존 파일을 읽을 수 없습니다: <path>` for a file that exists, and
`search_knowledge`/`doctor` report `OperationalError: unable to open database file`. Count the
open descriptors before suspecting permissions:
`for PID in $(pgrep -f pkb.mcp_server); do lsof -p $PID | wc -l; done`.

It starts automatically at login, and `KeepAlive` restarts it if it dies. Because of the
`RunAtLoad`+`KeepAlive` combination, **`pkill` won't stop it** — use `launchctl unload` instead.

You can also run the server directly (for development/debugging). Just **don't run two instances
at once** — a port conflict sends launchd into an infinite restart loop, and each failed startup
attempt eats memory trying to load the models.

```bash
uv run python -m pkb.mcp_server   # foreground
```

## 2. Registering Clients

All three clients support streamable HTTP. **The argument syntax differs between them.** The commands
below use the same endpoint regardless of the stateless transport; each client manages protocol
metadata and headers automatically.

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

### Running on a Separate Machine (LAN / VPN)

To keep the server on an always-on machine and connect from a laptop, open the bind address. Set
`MCP_HOST=0.0.0.0` in the server's `.env`, or put the same value in the launchd plist's
`EnvironmentVariables` (environment variables take precedence over `.env`, so you can leave `.env`
untouched). On the client, only the host part of the URL changes.

```bash
claude mcp add --transport http pkb http://192.168.0.5:8787/mcp -s user
```

Leave Elasticsearch on `network.host: 127.0.0.1`. Only the MCP server needs to be reachable.

> ⚠️ The MCP server has no authentication and its `write_file` tool can write anywhere in the vault.
> Open it only inside a home LAN or a VPN you operate. Do not port-forward it to the internet.

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
| `write_file` | Writes `.md` files under `data/`. MCP default is `ingest=False`; a batch is indexed once with `sync_corpus`. Documents still under review carry `status: draft` (excluded from `curated`/`evidence` search) |
| `read_file` | Returns the on-disk source of a `data/**.md` file plus its `content_hash` (`get_document` serves ES chunks, which cannot be reassembled into the original) |
| `patch_file` | Replaces one exact occurrence of `old` with `new` in an existing document — partial edits without resending the whole file. Same policy check and optimistic lock (`expected_hash`) as `write_file` |
| `list_documents` | Lists documents stored in ES. Shows the top `limit` (default 50) sorted by `date_modified` descending. Set `limit<=0` to list everything. Reads the document-level index when it exists, so it no longer times out without a `category` |
| `list_categories` | Returns `{"documents": N, "categories": {name: count}}` from one aggregation on the document-level index. Use it instead of `list_documents`/`doctor` when only the category list is needed |
| `related_documents` | Returns documents whose whole-document vector (mean of chunk embeddings) is closest to a given `doc_id`, excluding the document itself. Accepts the same scope filters as `search_knowledge` |
| `list_files` | Lists immediate files and folders under `data/`, including unindexed `_origin` attachments. Continue with `next_offset` |
| `get_file` | Transfers an original `data/` file as an embedded MCP resource containing a base64 blob, with name, MIME type, byte size and SHA-256. Maximum 10 MiB per file |
| `put_attachment` | Uploads a file outside the vault (`ATTACHMENTS_ROOT`, e.g. meeting audio) in chunks of up to 4 MiB. The file appears only after the last chunk's SHA-256 matches; existing files are never overwritten. Not indexed |
| `get_attachment` | Downloads a file under `ATTACHMENTS_ROOT` in chunks of up to 4 MiB as JSON text with `data_base64`; the last chunk carries the file's SHA-256 |
| `search_media` | Finds images and audio by a text description (kNN over media vectors only). Audio comes back per 30-second segment with `start`/`end`; several segments of one file can rank. See [Image and audio search](#image-and-audio-search) |
| `get_media` | Downloads an image or audio file returned by `list_media`/`search_media` in chunks of up to 4 MiB, same JSON shape as `get_attachment` plus `doc_id`, `kind`, `mime_type` |
| `list_media` | Lists images and audio under `data/`, the vault and `ATTACHMENTS_ROOT` with size, mtime, SHA-256 and whether current vectors exist. Used by `pkb media-sync` |
| `put_media_embeddings` | Replaces one file's media chunks with vectors computed by a client. Refuses unless the model, file hash, dimensions and vector space match |
| `add_document` | Ingests a file under `data/` (md/txt/pdf/docx/pptx/xlsx/html) |
| `convert_and_ingest` | Converts an external PDF/DOCX/PPTX/XLSX/HTML file to `.md`, saves it under `data/<category>/`, and ingests it. The converted file's frontmatter records provenance (`source`, `converted_from`, `converted_at`); PDFs keep page markers (`## p.N`) |
| `sync_corpus` | Reconciles the `data/` corpus with ES: upserts, then lists documents no longer in the corpus. Nothing is deleted unless `confirm_prune=True` — with a Syncthing-replicated corpus a freshly written file may not have arrived yet. Syncthing conflict copies (`*.sync-conflict-*`) are never indexed, and any that are still in the index are removed without confirmation |
| `sync_obsidian` | Reconciles the Obsidian vault with ES: upserts plus pruning of documents no longer in the vault. Categories are derived dynamically from the vault's top-level folder name (same rule as `data/`). If `OBSIDIAN_PATH` isn't set, it proposes pruning all remaining `obsidian/*` documents (`confirm_prune=True` required) |
| `get_document` | Fetches a document. By default (`include_content=False`), returns only metadata (title, category, modified date) plus a chunk table of contents. Use `chunk_range` (e.g. `"3-7"`) to get the full text of that range, or `include_content=True` for the full text |
| `reindex_document` | Re-reads a specific source document and re-ingests it into ES |
| `archive_document` | Archives a document as a soft delete (excluded from search, recoverable). For `.md` originals under `data/`, records `archived_at` in the frontmatter and re-ingests (the file is the SSOT); for everything else, the record is written only to ES |
| `restore_document` | Restores an archived document so it's searchable again. If the frontmatter has archive metadata, it's removed before re-ingesting |
| `doctor` | Checks ES connectivity, indices, chunk counts, configuration, and concept graph stats, and lists candidate actions (expired-but-not-archived documents, purge candidates, orphan concepts) |

`search_knowledge(expand_context=N)` attaches up to N neighboring chunks (0–4; omitted uses
`EXPAND_CONTEXT`). `max_context_tokens` (256–32000, default 4000) caps the rendered result,
including source labels, concept vocabulary, hit bodies, and neighboring context. A larger
`top_k` or expansion window does not bypass that output budget.

### Item metadata, scope filters and the document-level index

Ingest indexes these optional frontmatter keys on every chunk. Notes without them are unaffected.

| Key | Meaning |
|---|---|
| `source_type` | `meeting`, `event`, `email`, `drive`, `note` (stored lowercase) |
| `occurred_at` | ISO 8601 date or time the item happened. Unparseable values are dropped with a write warning |
| `people` | Attendees, senders, owners. Email addresses are lowercased; names are kept |
| `source_id`, `source_link` | ID and URL in the original service. `source_link` is stored but not searchable |
| `links` | doc_ids of related items |
| `attachments` | Paths relative to `ATTACHMENTS_ROOT` |

`search_knowledge` and `related_documents` accept the same scope filters, applied to both the
BM25 and kNN legs: `doc_ids` (search only inside these documents, e.g. the items linked to a
meeting), `source_types`, `people`, `date_from`/`date_to` (inclusive range on `occurred_at`; a
date without a time covers the whole UTC day, and documents without `occurred_at` drop out) and
`path_prefix` (doc_id prefix). Empty lists mean no restriction.

The document-level index (`<ES_INDEX>_docs`) holds one row per document with its metadata, chunk
count and `doc_vector`, the L2-normalized mean of the chunk embeddings. Ingest, delete, archive,
restore and purge keep it in sync once it exists. Create and fill it from the stored chunk vectors
(no re-embedding) with `uv run pkb doc-index`. Until then `list_documents` falls back to
aggregating chunks, and `list_categories`/`related_documents` return an error asking for it.

### Attachments outside the vault

Set `ATTACHMENTS_ROOT` (for example `/Volumes/PKBData/attachments`) to enable `put_attachment` and
`get_attachment`. Paths are relative to that folder; absolute paths, `..`, hidden components and
anything resolving outside it are rejected.

Upload: send chunks in order with `offset` equal to the bytes the server has received (the
`received` field of the previous response). `offset=0` restarts. The last chunk must carry the
whole file's `sha256`; on a match the file is moved into place atomically, otherwise the partial
upload is discarded. A path that already holds a file is never overwritten — retrying with the
same `sha256` reports completion.

Download: call `get_attachment(path, offset, length)` until `eof` is true, decode each
`data_base64`, and verify the concatenation against the `sha256` in the last response.

### Image and audio search

Images and audio are searched with text, in the same vector space as the documents. The server
never loads the media model: `pkb media-sync` on another Mac embeds the files with
`jinaai/jina-embeddings-v5-omni-small` and sends the vectors with `put_media_embeddings`. That
model's text tower is locked to `jinaai/jina-embeddings-v5-text-small`, so the query vector the
server computes with text-small finds them (same sentence through both models, Korean and English,
query and document prompts: cosine ≥ 0.9997, measured 2026-10).

Server settings:

```
EMBEDDING_MODEL=jinaai/jina-embeddings-v5-text-small   # with the other jina v5 settings, EMBEDDING_TASK=retrieval
MEDIA_EMBEDDING_MODEL=jinaai/jina-embeddings-v5-omni-small
```

An empty `MEDIA_EMBEDDING_MODEL` turns media indexing and search off. The server accepts a media
model only with the text model and adapter it shares a space with (`MEDIA_SPACES` in
`src/pkb/media.py`). With any other text model (bge-m3, for example) `put_media_embeddings` and
`search_media` refuse and `list_media` reports `"model": null`.

**Sources.** `data/…` (`DATA_ROOT`), `obsidian/…` (`OBSIDIAN_PATH` minus `DATA_ROOT`) and
`attachments/…` (`ATTACHMENTS_ROOT`). Images: png, jpg, jpeg, gif, webp, bmp, tif, tiff, heic,
avif, svg. Audio: m4a, mp3, wav, aiff, aif, flac, ogg, opus, aac. The exclusions are those of
Markdown ingest: hidden components (including `.uploads`), the reserved folders (`_trash`,
`_archive`, `_review`, …), Syncthing conflict copies, and symlinks. Markdown ingest never reads
media files.

**Chunks.** One per image, and one per audio window of at most 30 seconds (the model reads audio
through a 30-second Whisper window; the client drops near-silent windows). Media chunks live in the
chunk index next to the text chunks with `source_type` `image` or `audio`, `title` (file name
without extension), `category` (top folder under its root), `date_modified` (file mtime),
`media_start`/`media_end` (seconds, audio only), `media_sha256`, `media_model`, `linked_from` (the
notes that point at the file) and `content`, a short description shown as `context`: the path, plus
for each linked note its title, `occurred_at` and `people`, and for an image the lines around
`![[name]]` or `![](path)`. Audio under `ATTACHMENTS_ROOT` is linked by the `attachments` list in
a note's frontmatter. `occurred_at` and `people` come from the linked notes, so the date and people
filters work on media too.

**Keeping context current.** `sync_corpus`, `sync_obsidian` and `pkb sync` recompute the note
context of indexed media and rewrite only what changed, without re-embedding — renaming a meeting
note or editing its people moves `linked_from` and the filters along.

**Elsewhere.** `search_knowledge` and `related_documents` leave media out unless `source_types`
names `image` or `audio`. `list_documents` and `list_categories` never list media (use
`list_media`). Concept-graph extraction skips media chunks.

**Removal.** A media file that still exists is never reported as missing. When it disappears, its
doc_id joins the usual list: `data/` and `attachments/` in `sync_corpus` (deleted only with
`confirm_prune=True`), `obsidian/` in `sync_obsidian`, all three in `pkb sync`. If
`ATTACHMENTS_ROOT` is set but the folder is missing (an unmounted volume), nothing under
`attachments/` is pruned; if it is unset, every remaining `attachments/` entry is listed.

Responses (JSON text):

```
list_media   → {"total": N, "model": "<accepted media model>" | null,
                "media": [{"doc_id", "kind": "image"|"audio", "size_bytes", "modified" (ISO 8601 UTC),
                           "sha256", "indexed": bool, "model": "<model of stored vectors>" | null}]}
get_media    → {"doc_id", "kind", "mime_type", "offset", "length", "total_size", "eof",
                "data_base64", "sha256" (only when eof)}
put_media_embeddings → {"doc_id", "kind", "segments": N, "sha256", "model", "linked_from": [doc_id]}
search_media → {"query", "results": [{"doc_id", "kind", "title", "start", "end", "score",
                                      "context", "linked_from": [doc_id]}]}
```

`start`/`end` are seconds and `null` for images. `score` is Elasticsearch's cosine score
`(1 + cos) / 2`. `put_media_embeddings` takes `segments=[{"start", "end", "vector"}]`: one segment
without `start`/`end` for an image, non-overlapping windows of at most 30 s for audio (at most 1000),
each vector `EMBEDDING_DIMS` finite numbers (the server rescales it to unit length). `sha256` must
match the file as it is now.

**Client.** On a Mac with the model (`uv sync --extra media` adds torchvision, which only the client
needs):

```bash
uv run --extra media pkb media-sync --url https://<gateway>/mcp      # Bearer token from ~/.config/mcp-gateway/token (https only)
uv run --extra media pkb media-sync --url http://<server>:8787/mcp --dry-run
uv run --extra media pkb media-sync --kinds audio --limit 20
```

It lists files without current vectors (`list_media(missing_only=True)`), downloads each with
`get_media`, embeds it on the document side (`retrieval` adapter, MPS with bf16 where available,
loading only the vision or audio tower), and calls `put_media_embeddings`. Audio is decoded with
macOS `afconvert` to 16 kHz mono and cut into 30-second windows; SVG and HEIC go through `sips`.
A gateway's `pkb-…` tool names are detected from `tools/list`. Re-running does nothing for files
that are already current.

### Receiving original files on another client

`list_files` and `get_file` are available in the `core` profile too. Search for the relevant
document, list its linked source directory, and request the file:

```python
list_files(directory="data/about/_origin/personal-brand/ljk")
get_file(file_path="data/about/_origin/personal-brand/ljk/ljk-black-1024.png")
```

Listings are not recursive. When `next_offset` is present, use it as `offset` to fetch the next
page (100 entries by default, maximum 200). Hidden paths, symlinks and special files are excluded.
Absolute paths and paths outside `data/` are rejected.

`get_file` embeds the **original bytes in the response**, rather than returning a server-local
path. In the JSON result, find the `content` item with `type: resource` and base64-decode its
`resource.blob` to restore the exact PNG, SVG, PDF or other file. Metadata (`name`, `mime_type`,
`size_bytes`, `sha256`) is available in `structuredContent` and a short text block.
The `pkb://files/...` URI identifies the embedded payload; it is not an HTTP download URL.

Attachment display and save buttons depend on the client. An agent with file tools can decode
the blob, save it, and verify its size and SHA-256. A 10 MiB original produces approximately
13.3 MiB of base64, so client and gateway response limits also apply. Neither tool modifies
the corpus or search index.

After updating the server, restart the PKB process and refresh its tool inventory in any
gateway. Reconnect MCP or start a new conversation if the client caches the old tool list.
With MCP ContextForge, restarting the gateway does not re-discover tools; call
`POST /gateways/<gateway id>/tools/refresh` on its admin API (for example
`curl -X POST -H "Authorization: Bearer $(cat ~/.local/share/mcpgateway/.admin-token)" http://127.0.0.1:4444/gateways/<id>/tools/refresh`).

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

Graph RAG's MCP-first workflow: read chunks with `graph_list_chunks`, have Claude Code extract concepts and relations directly, and store them with `graph_store_concepts`. After storing, label real/vocab with `graph_curate` and merge notation variants with `graph_merge`. Query SQLite directly with `graph_explain`/`graph_path`/`graph_query`/`graph_affected`, or render an offline HTML view with `graph_map`.

Copy each chunk's `content_hash` and `input_hash` from `graph_list_chunks` unchanged into its
`graph_store_concepts` item, including items with `concepts: []`. The input hash covers the source
identity, chunk position, content, title, section, and category used for extraction. A changed
input makes the chunk pending again; storing an older pair is rejected, so fetch and extract it again.

### Parameter Reference

Exact signatures as registered in `src/pkb/mcp_server.py`:

```python
search_knowledge(query, category="", top_k=5, include_archived=False,
                 include_obsidian=True, query_variants=[], profile="all",
                 canonical_group=True, canonical_boost=0.15,
                 expand_context=None, max_context_tokens=4000,
                 doc_ids=None, source_types=None, people=None,
                 date_from=None, date_to=None, path_prefix=None)
read_file(file_path)
patch_file(file_path, old, new, expected_hash="", ingest=False, dry_run=False, strict_policy=True)
write_file(file_path, content, ingest=False, dry_run=False,
           expected_hash="", strict_policy=True)
list_documents(category="", include_archived=False, limit=50)
list_categories(include_archived=False)
related_documents(doc_id, top_k=5, include_archived=False, doc_ids=None, source_types=None,
                  people=None, date_from=None, date_to=None, path_prefix=None)
list_files(directory="data", offset=0, limit=100)
get_file(file_path)
put_attachment(path, data_base64, offset, total_size, sha256="")
get_attachment(path, offset=0, length=4194304)
list_media(kinds=None, missing_only=False, limit=200, offset=0)
get_media(doc_id, offset=0, length=4194304)
put_media_embeddings(doc_id, kind, sha256, model, segments)
search_media(query, kinds=None, top_k=12, include_archived=False, doc_ids=None, people=None,
             date_from=None, date_to=None, path_prefix=None)
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
- *"Bring the data corpus up to date"* → `sync_corpus()` (upsert; stale documents are only listed — pass `confirm_prune=True` to delete them)
- *"Also sync the vault files left outside the corpus"* → `sync_obsidian()` (uses OBSIDIAN_PATH from .env)

`category` is required for `convert_and_ingest`. Claude Code infers it from the filename, user instructions, or content context. Category = the corpus's top-level folder name (dynamic); existing categories are reused when possible, and a new name creates a new category folder. You can also specify a subfolder path like `study/payments` (the category is the first folder).

### Document Management

- *"Show me the list of stored career documents"* → `list_documents(category="career")`
- *"Just show me the table of contents for this document: `data/study/rag/xxx.md`"* → `get_document(doc_id="data/study/rag/xxx.md")` (default is metadata + TOC)
- *"Show me the full content of this document"* → `get_document(doc_id="...", include_content=True)`
- *"Re-index the document I just edited"* → `reindex_document(doc_id="data/study/rag/xxx.md")`

### File Creation

- *"Find only canonical knowledge"* → `search_knowledge(profile="curated")`
- *"Include the supporting research"* → `search_knowledge(profile="evidence")`
- *"Create a summary note based on what you found"* → search → choose path and canonical ID →
  `write_file(dry_run=True)` → inspect diff → apply with `expected_hash` → sync once after the batch

### Concept Graph

- *"Build the concept graph for the rag material, 20 chunks at a time"* → repeated `graph_list_chunks(category="rag", limit=20)` + `graph_store_concepts`
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
    ├─ write_file            → policy check + diff/hash preview → write .md under data/
    ├─ read_file / patch_file → read on-disk source + hash / replace one exact span (same policy + lock as write_file)
    ├─ add_document          → chunking → embedding → ES indexing
    ├─ convert_and_ingest    → convert to md (pdfminer preserves pages for PDF, markitdown for others) → save to data/ → ES indexing
    ├─ sync_corpus / sync_obsidian → reconcile with source (upsert; sync_corpus only deletes with confirm_prune=True)
    ├─ get_document          → look up chunks/section_path per document
    └─ graph_*               → query/store the SQLite concept graph
    ↓
Tool results feed back into the Claude Code conversation context
```

Within MCP, Claude Code itself handles tool selection, re-search, summarization, and file writing.

## Notes

- The data corpus's actual location is determined by `DATA_ROOT` (default: `data/` inside the project). Regardless of that location, tool input paths and doc_id always use the `data/...` format.
- `write_file` and `add_document` only accept paths under the corpus (`data/...`).
- `write_file` only accepts `.md` files; its MCP default is `ingest=False`. Existing files must first
  be previewed with `dry_run=True`, then applied using the returned `previous_hash` as `expected_hash`.
- Writes to `concepts/`, `guides/`, `research/`, and `00_MOC.md` enforce the document-contract frontmatter.
- `convert_and_ingest` places no restriction on where the source file lives, but the converted output is always saved under the corpus's `<category>/`.
- `sync_corpus` is the default reconciliation tool (upserts the entire `data/` corpus + prunes stale documents). `sync_obsidian` is an optional path used only when vault files are left outside `DATA_ROOT`: it reads the external Obsidian vault and writes only to ES (the source files are never copied or modified). If `DATA_ROOT` sits inside the vault, that subtree is excluded from the crawl (to avoid double ingestion). `sync_corpus` never deletes without `confirm_prune=True` (it lists stale documents instead); `sync_obsidian` prunes small sets automatically and requires `confirm_prune=True` for 21+ deletions.
- Large-scale Graph RAG construction can take a while, so start with a specific category (e.g. `rag`) or a single `doc_id`.

Search also accepts `analyze`, `issues`, `needed_sources`, and legal filters `as_of`, `law_id`, `article_id`, `case_id`, `legal_version`, `legal_kind`. The visible plan and legal provenance share the context token budget. Consumers should cite each factual claim, withhold answers lacking support, and describe conflicting sources with their dates and scope. See [question/answer evaluation](../evaluation/README.md) and [legal metadata](legal-metadata.md).
