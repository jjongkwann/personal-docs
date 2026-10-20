# Access Control

**English** | [한국어](ko/access-control.md)

PKB can restrict what each caller reads and writes. Every read path enforces the restriction: search, document and file reads, listings and counts, media, and the concept graph. This document covers the model, how each path enforces it, what still leaks, how to operate it, and how the design changes when it serves a company instead of one person.

Implementation: `src/pkb/access.py` (policy, principals, ES filter, audit) and `src/pkb/graph/visibility.py` (graph views). Tests: `tests/test_access*.py`.

## Modes

| `ACCESS_POLICY` | Behaviour |
| --- | --- |
| unset (default) | Single-user mode. No authentication; every call runs as the built-in admin `system`. Same behaviour as before this feature. |
| path to a YAML file | MCP requests need `Authorization: Bearer <token>`. The token selects a principal, and path rules decide what that principal can read and write. Anything no rule grants is denied. |

If the file is set but cannot be read or parsed, the server refuses to start, and running servers reject every request (fail closed).

## Model

- **Resource**: a doc_id-style path: `data/…`, `obsidian/…`, `attachments/…`. Files, indexed chunks, document rows, media and graph evidence all carry one.
- **Principal**: an entry under `principals` with optional `groups`, `admin`, and SHA-256 hashes of its bearer tokens.
- **Subject** (the entries in rule lists): `user:<id>`, `group:<name>`, or `*` (any authenticated principal).
- **Rule**: `path` plus `read` and `write` subject lists. The longest rule that matches a resource decides alone, so a subfolder rule overrides its parent in either direction. `path` matches whole path segments: `data/career` covers `data/career/x.md` but not `data/careerist.md`. `default` is the rule for everything else; omitting it denies by default.
- `write` implies `read`. An editor has to see the file to produce a diff and an `expected_hash`.
- `admin: true` principals bypass the rules and may call admin tools.

```yaml
version: 1
default:            # anything no rule covers; omit to deny
  read: [group:owner]
  write: [group:owner]
principals:
  jk:
    groups: [owner]
    admin: true
    token_sha256: <sha256 of jk's token>
  study-agent:
    groups: [study]
    token_sha256: [<hash>, <hash of a rotated token>]
rules:
  - path: data/study
    read: [group:owner, group:study]
  - path: data/study/private      # the longest match wins: closes a subfolder again
    read: [group:owner]
  - path: data/career
    read: [user:jk]
    write: [user:jk]
```

Rules apply to paths, not to frontmatter, for three reasons. Binary files, attachments and media have no frontmatter. A caller with write access must not be able to widen a document's readers by editing it. And a rule change takes effect on the next request, with no re-index.

## Enforcement points

| Path | Mechanism |
| --- | --- |
| Hybrid search (BM25 + kNN + neighbours), media kNN, related documents, document fetch | The policy is compiled into one Elasticsearch filter (`Policy.es_filter`), which `_search_filters` adds to both the BM25 `bool.filter` and the `knn.filter` before fusion. It filters before ranking, so top-k is filled with readable chunks only. A filter applied to results afterwards would leave fewer than k. |
| Result lists | Every result passes `access.readable()` as well. Dropping a row would mean the compiled filter disagrees with the policy, so it also logs a warning. |
| `list_documents`, `list_categories` | The same filter sits in the main query, so aggregation counts only cover readable documents. |
| `read_file`, `get_file`, `list_files`, `write_file`, `patch_file`, attachments, `get_media`, archive/restore/reindex | `require()` checks the resource before the file's existence is examined. A denied resource and a missing one return the same message, so existence cannot be probed. |
| Concept graph tools | `graph_read_connection` creates TEMP views named like the graph tables, which shadow the main tables (SQLite resolves unqualified names in `temp` first). Existing query code reads the filtered graph unchanged, and the views reject writes. |
| Tool classes | Every MCP tool is declared `read`, `write` or `admin` (`@tool_access`). A test fails if a tool lacks a class. Admin tools: sync, convert, doctor, graph extraction/curation/merge. |
| Identity | MCP SDK `TokenVerifier` (`PolicyTokenVerifier`). The principal comes only from the token and is never taken from tool arguments. The policy is re-read on each request, so removing a principal, a token hash or a group membership takes effect immediately. |
| Audit | `<DATA_ROOT>/.logs/access.jsonl` records each tool call (principal, tool, class, decision) and each denied resource. It contains no document content. `.logs` is a hidden folder, so no file tool can read it. |

### Case-insensitive file systems

On macOS the default file system opens `data/CAREER/x.md` as `data/career/x.md`. If rules were compared against the request string, changing case would skip a deny rule. File-backed checks therefore rebuild the resource from the real directory entries, compared after NFC normalization. A request that differs from an existing entry only in case is refused. `..` and empty segments are rejected before any rule comparison.

### Graph visibility rules

The concept graph is derived from many documents. Filtering only the result list would still let descriptions, aliases, counts and paths reveal private documents.

- Mentions, edge evidence, document rows and extraction markers: only rows whose `doc_id` is readable.
- Concept: visible if at least one readable mention or edge evidence exists. Name, slug, category and kind are shown because they come straight from that chunk. Description, embedding, aliases and distilled prose combine several documents, so they appear only when *every* source of the concept is readable. A new column stays hidden until it is added to `PUBLIC_CONCEPT_COLUMNS` (fail closed).
- Edge: visible if it has readable evidence; `weight`, `evidence_count` and `confidence` are recomputed from that evidence.
- Inferred (transitive) relations are computed over the visible graph, so every premise is visible.
- Alias-conflict records are hidden.

Media search results get the same treatment. A media chunk's context comes from the notes that embed the file, so the context is shown only when all linking notes are readable, and `linked_from` lists only readable notes.

## What still leaks (known limits)

- **BM25 statistics**: IDF is computed over the whole shard, so scores carry a weak signal about how many hidden documents contain a term. Elasticsearch DLS has the same limitation. Removing it requires separate indexes.
- **Admin is a superuser**: there is no operator role that can run sync without reading content.
- **Static tokens never expire.** Rotate them by adding a second hash and removing the old one.
- **Audit write failures do not block requests.**
- **Prompt injection**: a document shared by a lower-trust principal can carry instructions. PKB marks retrieved content as data in its instructions; the client still has to treat it that way.
- **No per-item ACLs** (frontmatter or connector-provided). See stage 2 below.

## Operating it

```bash
uv run pkb access token                              # prints a new token and its sha256
uv run pkb access show                               # principals, rules, indexed documents per rule (0 = path typo)
uv run pkb access check data/career/cv.md -p study-agent
uv run pkb --as study-agent query "hybrid search"    # see what a principal sees (read commands only)
```

1. Write the policy and put only token hashes in it. Give each client its token through its MCP configuration (`Authorization: Bearer …`).
2. Set `ACCESS_POLICY=/path/to/access.yml`, and `MCP_PUBLIC_URL` if clients reach the server through another URL, then restart the MCP server.
3. A client can call `whoami` to see its principal and the rules that grant it access.

With a gateway in front of PKB, PKB authenticates whatever token the gateway sends. A gateway that uses one PKB token for all its users makes them a single principal. Per-user enforcement needs per-user tokens to reach PKB, or token exchange at the gateway (see stage 1).

## Scaling to a company

The single-machine design has one seam that the later stages replace: the function that turns "this principal" into "these resources" (`Policy.es_filter`, `Policy.allows`). The table lists what changes at each stage.

| Stage | Identity | Where ACLs come from | Enforcement | Revocation |
| --- | --- | --- | --- | --- |
| 0 — now | Static hashed tokens | Path rules in one file | Compiled ES pre-filter + per-row check; graph views | Next request |
| 1 — team | OIDC IdP (Keycloak, Entra ID, Okta). A JWT `TokenVerifier` checks the JWKS signature, `iss`, `exp` and an `aud`/resource bound to this server (RFC 8707); short-lived tokens. Groups come from the directory, not only from token claims: Entra stops listing groups after 200 and emits an overage marker. | Same rules, with IdP groups | Same | Group changes immediately; tokens within their lifetime |
| 2 — company | SCIM (RFC 7644) directory sync | Connectors import SharePoint, Drive, Confluence and Slack ACLs as principal and group ids (not expanded member lists) onto each item; path rules become a ceiling | Item ACL ids denormalized onto every chunk (`acl_read` keyword) and matched with a `terms` filter against the caller's expanded groups; deny entries win; read-time re-check for sensitive tools | A stated SLA per source (e.g. ≤ 15 min); identity revocation immediate |
| 3 — enterprise | Same, plus CAEP/SSF session revocation | Central ReBAC (OpenFGA, SpiceDB) for hierarchy and sharing; ABAC (Cedar, OPA partial evaluation) for attributes such as classification | Local ACL index fed from the authorization service's change feed, plus a final check on returned items (OpenFGA's "search with permissions" option 2); per-tenant indexes or clusters for hard isolation; ES DLS (Enterprise licence) as a second layer | Consistency tokens (Zanzibar zookies) to avoid the new-enemy problem |

What each stage adds beyond search:

- **Gateway**: never forward a client's token to PKB (MCP forbids token passthrough). Exchange it for a PKB-audience token (RFC 8693), or authenticate the gateway and pass a signed user assertion.
- **Derived data**: apply the graph rules above to every aggregate, i.e. summaries, centroids, counts and caches. A multi-document summary inherits the *intersection* of its sources' ACLs. Key caches by principal set and policy version.
- **Logs and evaluation sets**: they contain queries and snippets, so they are admin-only data with their own retention. Send the audit log to an append-only store or SIEM.
- **Tests that carry over**: equivalence between the compiled filter and the policy on random policies (`test_es_filter_selects_exactly_the_allowed_documents_on_random_policies`); a canary string in a restricted document that must not appear in any tool output; "every tool declares a class"; non-interference (changing hidden documents must not change another principal's results, apart from score drift).

Trade-offs:

| Option | Gain | Cost |
| --- | --- | --- |
| Compile policy to a filter (stage 0) | No ACL copies, immediate revocation | Works only while policies are path-shaped and small |
| Denormalized item ACLs (stage 2) | Correct top-k at low latency for millions of distinct ACLs | ACLs only as fresh as the last sync |
| Filter results after search against a policy engine | Always fresh | Shrinks top-k, adds latency |
| Per-tenant index | Strong isolation, removes the IDF side channel | Operational overhead, no cross-tenant search |
| ES document-level security | Enforced inside the engine | Paid licence; per-user roles or API keys |
