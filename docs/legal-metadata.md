# Legal source metadata

Legal metadata is optional YAML frontmatter on Markdown source files. It is
copied to every indexed chunk together with the existing `source_path`,
`section_path`, `chunk_index`, and content hash. Identifiers are trimmed NFC
strings; the source's identifier format is preserved.

```yaml
---
legal_kind: statute
law_id: fixture-law
article_id: 제12조의2
legal_version: revision-2024
effective_from: 2024-01-01
effective_to: 2025-01-01
original_url: https://example.org/law/revision-2024
original_location: 제12조의2 제1항
---
# 제12조의2
Exact source paragraph.
```

This example is synthetic metadata, not a statement about a real law. Store
each historical revision in a different source file, for example
`laws/fixture-law/revision-2024.md` and `revision-2025.md`. Path-based document
IDs keep both versions indexed. Ingest rejects a change to an already known legal identity,
version, start date, or judgment date at the same path before writing to ES. A new revision
needs a separate file; this prevents replacing historical indexed evidence. Corrections to
content within the same revision and closing its effective interval remain ordinary updates.
Use a separate article file when article-specific effective dates or identifiers
differ. `original_location` records the source paragraph/page/anchor verbatim;
`section_path` additionally identifies the indexed chunk's local section.

| Field | Meaning |
| --- | --- |
| `legal_kind` | `statute` or `judgment`; required when legal identity, version, or date metadata is supplied |
| `law_id` | Source law identifier; required for statutes |
| `article_id` | Source article identifier, such as `제12조의2` |
| `case_id` | Source judgment/case identifier; required for judgments |
| `legal_version` | Source revision identifier; no version is guessed |
| `effective_from` | Known first effective date, inclusive |
| `effective_to` | Known end of this version's effective interval, exclusive |
| `decision_date` | Judgment date; separate from a statute's effective date |
| `original_url` | Absolute HTTP(S) source URL |
| `original_location` | Original article, paragraph, page, or anchor location |

`original_url` and `original_location` alone are also valid on ordinary technical sources and
do not turn them into legal documents.

All dates must be calendar-valid `YYYY-MM-DD`; YAML date values are supported.
Unknown fields stay absent, including unknown dates. `effective_to` requires a
known `effective_from` and must be later. Null `effective_to` represents an
unbounded interval, not evidence that the version is currently authoritative.
Missing dates must not be treated as proof of applicability at a reference date.

Metadata-only changes use partial updates without replacing embeddings. Missing
optional keys preserve existing indexed values, including during whole-chunk
replacement or slot movement. Explicit YAML `null` clears a supplied field;
identity validation still applies to the resulting stored metadata. To remove
all legal metadata, explicitly null every legal field; simply deleting the
frontmatter does not erase indexed provenance.

## Existing index migration

New indices include keyword mappings for identifiers and strict-date mappings
for legal dates. Existing indices are never migrated automatically by ingest.
After obtaining approval for the shared service change, call
`pkb.store.migrate_legal_mapping(es, index="explicit-physical-index")`. The
function rejects aliases, wildcard targets, and conflicting field types, then
adds only missing mappings. It does not delete/rewrite documents or infer
metadata for historical chunks. Re-ingest source files carrying verified
frontmatter to backfill the fields through ordinary metadata deltas.

If dynamic mapping already assigned a conflicting field type, build a separate
index with `INDEX_SETTINGS`, preserve the old index, and re-ingest verified
sources before switching the read alias. An old mapping cannot safely be
changed in place. Existing legacy sources lacking legal metadata stay
searchable but cannot establish date-specific legal applicability.
