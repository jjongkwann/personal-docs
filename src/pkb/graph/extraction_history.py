"""Durable generation provenance shared by incremental and full rebuild runners."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pkb.config import settings
from pkb.graph.schema import graph_connection


def now() -> str:
    return datetime.now(UTC).isoformat()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def start_run(
    conn, *, run_id, endpoint, model, digest, options, prompt_version, schema_version, prompt, schema, pending
):
    stamp = now()
    conn.execute(
        "INSERT INTO graph_extraction_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            stamp,
            stamp,
            "running",
            endpoint,
            model,
            digest,
            json_text(options),
            prompt_version,
            schema_version,
            text_hash(prompt),
            text_hash(json_text(schema)),
            prompt,
            pending,
            None,
        ),
    )


def add_items(conn, run_id: str, chunks: list[dict]):
    conn.executemany(
        "INSERT OR IGNORE INTO graph_extraction_items "
        "(run_id, doc_id, chunk_index, content_hash, input_hash, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                c["doc_id"],
                c["chunk_index"],
                c.get("content_hash") or "",
                c.get("input_hash") or "",
                "pending",
                now(),
            )
            for c in chunks
        ],
    )


def update_items(run_id: str, keys: list[tuple[str, int]], status: str, *, error=None, result=None, attempt=False):
    with graph_connection(settings.graph_db_path) as conn:
        encoded = json_text(result) if result is not None else None
        stamp = now()
        conn.executemany(
            "UPDATE graph_extraction_items SET status = ?, updated_at = ?, error = ?, result_json = ?, "
            "attempts = attempts + ? WHERE run_id = ? AND doc_id = ? AND chunk_index = ?",
            [(status, stamp, error, encoded, int(attempt), run_id, *key) for key in keys],
        )
        conn.executemany(
            "INSERT INTO graph_extraction_events "
            "(run_id, doc_id, chunk_index, status, created_at, error, result_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, *key, status, stamp, error, encoded) for key in keys],
        )


def finish_run(run_id: str, status: str, error: str | None = None):
    with graph_connection(settings.graph_db_path) as conn:
        conn.execute(
            "UPDATE graph_extraction_runs SET status = ?, updated_at = ?, error = ? WHERE run_id = ?",
            (status, now(), error, run_id),
        )
