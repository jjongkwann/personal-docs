"""Cache compact graph input versions until the source index changes."""

from __future__ import annotations

from dataclasses import dataclass, field

from pkb.config import settings
from pkb.graph import store as graph_store
from pkb.graph.services import scan_pending_chunks


def _snapshot(es) -> tuple:
    # seq_no is a response field, not a valid indices.stats metric in ES 8.17.
    response = es.indices.stats(
        index=settings.es_index,
        metric="docs",
        level="shards",
        filter_path="_shards,indices.*.uuid,indices.*.shards.*.routing,"
                    "indices.*.shards.*.seq_no",
    )
    if response.get("_shards", {}).get("failed", 0):
        raise RuntimeError("Graph discovery could not read all source shard statistics")
    versions = []
    for index, record in sorted(response["indices"].items()):
        uuid = record["uuid"]
        for shard, copies in sorted(record["shards"].items()):
            primaries = [copy for copy in copies if copy["routing"]["primary"]]
            if len(primaries) != 1 or primaries[0]["routing"]["state"] != "STARTED":
                raise RuntimeError("Graph discovery source primary is not available")
            primary = primaries[0]
            seq = primary["seq_no"]
            versions.append((index, uuid, shard, seq["max_seq_no"],
                             seq["local_checkpoint"], seq["global_checkpoint"]))
    if not versions:
        raise RuntimeError("Graph discovery returned no source primary statistics")
    return tuple(versions)


@dataclass
class DiscoveryCache:
    """One worker's source descriptors; no document bodies or embeddings survive a scan.

    A changed source is refreshed before discovery so even writes made without
    refresh=True cannot become permanently hidden behind an unchanged checkpoint.
    """

    sources: list[dict] = field(default_factory=list)
    identity: tuple | None = None
    snapshot: tuple | None = None

    def invalidate(self) -> None:
        self.snapshot = None

    def pending(self, es, conn) -> tuple[list[dict], int, bool]:
        """Return candidates, total and whether this discovery saw a stable source.

        A scan raced by ingestion still supplies useful candidates: the worker
        validates each selected version before extracting it. It cannot certify
        absence, retire retries, or settle the cache until a quiet scan succeeds.
        """
        identity = (settings.es_index, settings.graph_db_path)
        try:
            before = _snapshot(es)
            if identity != self.identity or before != self.snapshot:
                sources = []

                def capture(source, input_hash):
                    sources.append({"doc_id": source["doc_id"], "chunk_index": source["chunk_index"],
                                    "content_hash": source.get("content_hash"), "input_hash": input_hash})

                # The checkpoint can include unrefreshed writes. Refresh only
                # when it changes, then verify no writes raced this whole scan.
                refreshed = es.indices.refresh(index=settings.es_index)
                if refreshed.get("_shards", {}).get("failed", 0):
                    raise RuntimeError("Graph discovery could not refresh all source shards")
                scan_pending_chunks(es, conn, on_source=capture)
                after = _snapshot(es)
                self.sources = sources
                self.identity = identity
                self.snapshot = after if before == after else None
        except Exception:
            self.invalidate()
            raise
        content, _ = graph_store.extracted_markers(conn)
        inputs = graph_store.extracted_input_markers(conn)
        pending = [source for source in self.sources
                   if not source["content_hash"]
                   or content.get((source["doc_id"], source["chunk_index"])) != source["content_hash"]
                   or inputs.get((source["doc_id"], source["chunk_index"])) != source["input_hash"]]
        return pending, len(self.sources), self.snapshot is not None
