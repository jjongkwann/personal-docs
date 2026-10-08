"""Opt-in real ES validation, restricted to a disposable local test cluster.

Run against a separate server (never the configured PKB service):
PKB_ES_TEST_HOST=http://127.0.0.1:19201 pytest -q tests/test_legal_es_integration.py
The cluster name must start with pkb-legal-verification-.
"""

import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.legal import LEGAL_MAPPING_PROPERTIES
from pkb.retrieve import hybrid_search
from pkb.store import INDEX_SETTINGS, add_chunks, migrate_legal_mapping

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("PKB_ES_TEST_HOST"), reason="PKB_ES_TEST_HOST requires an isolated test cluster"),
]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "legal"
VECTOR = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture
def isolated_es():
    host = os.environ["PKB_ES_TEST_HOST"]
    parsed = urlsplit(host)
    # Validate before any request and independently of production configuration.
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.port != 19201:
        pytest.fail("legal integration requires loopback port 19201; production hosts are forbidden")
    es = Elasticsearch(host)
    try:
        assert es.info()["cluster_name"].startswith("pkb-legal-verification-"), "not a disposable legal test cluster"
        yield es
    finally:
        es.close()


def _rows(name):
    return [json.loads(line) for line in (FIXTURE_ROOT / name).read_text().splitlines() if line.strip()]


def test_real_es_historical_hybrid_search_variants_neighbors_and_preservation(isolated_es, monkeypatch):
    es = isolated_es
    index_name = f"pkb-legal-integration-{uuid4().hex}"
    monkeypatch.setattr(settings, "es_index", index_name)
    monkeypatch.setattr("pkb.retrieve.embed", lambda queries: [VECTOR for _ in queries])
    index_settings = copy.deepcopy(INDEX_SETTINGS)
    index_settings["mappings"]["properties"]["embedding"]["dims"] = len(VECTOR)
    index_settings["settings"]["number_of_replicas"] = 0
    corpus = _rows("corpus.jsonl")
    chunks = [{**row, "embedding": VECTOR, "canonical_id": "fictional-law-7"} for row in corpus]
    # Today's archive/expiry does not invalidate a historical legal source.
    chunks[0].update(archived_at="2026-02-01", expires_at="2026-02-01")
    diagnostic_chunks = [
        {**chunks[0], "chunk_index": 1, "content": "가상 부속 근거 문단", "original_location": "가상 원문 제2문단"},
        # Deliberately conflicting adjacent metadata proves context cannot leak
        # a version that the main search excluded by its validity interval.
        {**chunks[0], "chunk_index": 2, "effective_from": "2026-01-01", "effective_to": "2027-01-01",
         "legal_version": "future-adjacent", "content": "기준일 이후의 인접 청크"},
    ]
    try:
        es.indices.create(index=index_name, body=index_settings)
        assert add_chunks(es, chunks) == 5
        assert es.count(index=index_name)["count"] == 5
        for case in _rows("gold.jsonl"):
            filters = {field: case[field] for field in (
                "as_of", "law_id", "article_id", "case_id", "legal_version", "legal_kind",
            ) if field in case}
            hits = hybrid_search(
                es, case["query"], top_k=10, candidate_k=10, canonical_group=True,
                variants=["통지 기간 가상문서열람법"], log=False, **filters,
            )
            expected = {row["doc_id"] for row in case["relevant"]}
            assert {row["doc_id"] for row in hits} == expected, case["id"]

        # The synthetic future neighbor is not a member of the five-row gold
        # corpus; add it only after scoring that corpus, then test isolation.
        assert add_chunks(es, diagnostic_chunks) == 2
        ids = [f"{row['doc_id']}_{row['chunk_index']}" for row in chunks + diagnostic_chunks]
        before = {row["_id"]: row["_source"] for row in es.mget(index=index_name, ids=ids)["docs"]}
        plans = []
        hits = hybrid_search(
            es, "2025-12-31 기준 제7조 조문 통지 기간?", law_id="FICTIONAL-EVAL-LAW-001",
            analyze=True, query_plan_out=plans, expand_context=2, top_k=1, log=False,
        )
        assert plans[0]["as_of"] == "2025-12-31"
        assert hits[0]["legal_version"] == "v1"
        assert hits[0]["neighbors"]
        assert all(row["legal_version"] == "v1" for row in hits[0]["neighbors"])
        assert all(row["chunk_index"] != 2 for row in hits[0]["neighbors"])
        assert all(row["original_location"] for row in hits[0]["neighbors"])
        assert all(row["_id"] for row in hits[0]["neighbors"])

        # Judgment-date boundary is inclusive, but the previous day is absent
        # in the gold loop above. A missing statute start never qualifies.
        judgments = hybrid_search(
            es, "가상 사건 판결", as_of="2026-03-01", case_id="2026다70001", log=False,
        )
        assert [row["case_id"] for row in judgments] == ["2026다70001"]
        undated = hybrid_search(
            es, "99일", as_of="2026-03-01", legal_version="undated", log=False,
        )
        assert undated == []
        after = {row["_id"]: row["_source"] for row in es.mget(index=index_name, ids=ids)["docs"]}
        assert before == after
        assert es.count(index=index_name)["count"] == 7
    finally:
        es.indices.delete(index=index_name, ignore_unavailable=True)


def test_real_es_legal_mapping_migration_is_additive_and_idempotent(isolated_es):
    es = isolated_es
    index_name = f"pkb-legal-legacy-{uuid4().hex}"
    legacy = {"doc_id": "legacy-document", "content": "preserve original source", "custom_metadata": "preserve"}
    try:
        es.indices.create(index=index_name, settings={"number_of_replicas": 0})
        es.index(index=index_name, id="legacy:0", document=legacy, refresh=True)
        assert set(migrate_legal_mapping(es, index=index_name)) == set(LEGAL_MAPPING_PROPERTIES)
        assert migrate_legal_mapping(es, index=index_name) == []
        assert es.get(index=index_name, id="legacy:0")["_source"] == legacy
        assert es.count(index=index_name)["count"] == 1
        properties = es.indices.get_mapping(index=index_name)[index_name]["mappings"]["properties"]
        for field, expected in LEGAL_MAPPING_PROPERTIES.items():
            assert properties[field]["type"] == expected["type"]
            if expected["type"] == "date":
                assert properties[field]["format"] == "strict_date"
    finally:
        es.indices.delete(index=index_name, ignore_unavailable=True)
