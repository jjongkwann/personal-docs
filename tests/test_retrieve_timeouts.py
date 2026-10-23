"""Exercise the real Elasticsearch retry transport without production services."""

import json
from unittest.mock import Mock

import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders, Urllib3HttpNode
from elastic_transport._node._base import NodeApiResponse
from elasticsearch import ConnectionTimeout, Elasticsearch

from pkb.retrieve import _attach_neighbors, _rrf_search, hybrid_search


@pytest.fixture
def transport(monkeypatch):
    calls = []
    outcomes = []

    def perform(node, method, target, **kwargs):
        calls.append((method, target, kwargs))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        meta = ApiResponseMeta(200, "1.1", HttpHeaders({
            "content-type": "application/json", "x-elastic-product": "Elasticsearch",
        }), 0.001, node.config)
        return NodeApiResponse(meta, json.dumps(outcome).encode())

    monkeypatch.setattr(Urllib3HttpNode, "perform_request", perform)
    monkeypatch.setattr("pkb.retrieve.embed_queries", lambda queries: [[0.0] for _ in queries])
    with Elasticsearch("http://localhost:9200") as es:
        yield es, outcomes, calls


def response(*, timed_out=False):
    return {"timed_out": timed_out, "hits": {"hits": [{
        "_id": "a_0", "_score": 1.0,
        "_source": {"doc_id": "data/a.md", "chunk_index": 0, "content": "result"},
    }]}}


def test_one_transport_timeout_retries_and_returns_results(transport):
    es, outcomes, calls = transport
    outcomes.extend([ConnectionTimeout("temporary stall"), {"responses": [response(), response()]}])
    hits = hybrid_search(es, "q", log=False)
    assert [hit["doc_id"] for hit in hits] == ["data/a.md"]
    assert len(calls) == 2
    assert all(call[2]["request_timeout"] == 20 for call in calls)
    assert calls[0][2]["body"] == calls[1][2]["body"]
    # options() must not enable timeout retries on the shared client used by writes.
    assert es._retry_on_timeout is not True


def test_exhausted_transport_timeout_raises_instead_of_empty_results(transport):
    es, outcomes, calls = transport
    outcomes.extend(ConnectionTimeout("stalled") for _ in range(3))
    with pytest.raises(TimeoutError, match="Elasticsearch") as caught:
        hybrid_search(es, "q", log=False)
    assert len(calls) == 2
    assert isinstance(caught.value.__cause__, ConnectionTimeout)


@pytest.mark.parametrize("channel", [0, 1])
def test_rrf_rejects_server_timeout_with_partial_hits(transport, channel):
    es, outcomes, calls = transport
    responses = [response(), response()]
    responses[channel]["timed_out"] = True
    outcomes.append({"responses": responses})
    with pytest.raises(TimeoutError, match="일부 결과"):
        _rrf_search(es, "q", [0.0], None, 5)
    assert len(calls) == 1


def test_neighbors_retry_timeout_and_reject_server_partial_response(transport):
    es, outcomes, calls = transport
    outcomes.extend([ConnectionTimeout("temporary stall"), {"responses": [response(timed_out=True)]}])
    with pytest.raises(TimeoutError, match="이웃 청크"):
        _attach_neighbors(es, [{"doc_id": "data/a.md", "chunk_index": 0}])
    assert len(calls) == 2


def test_neighbors_propagate_subsearch_error():
    es = Mock()
    es.options.return_value.msearch.return_value = {"responses": [{"error": {"type": "bad_query"}}]}
    with pytest.raises(RuntimeError, match="이웃 청크 검색 실패"):
        _attach_neighbors(es, [{"doc_id": "data/a.md", "chunk_index": 0}])
