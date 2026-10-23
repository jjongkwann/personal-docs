import math

import pytest

from pkb import embeddings
from pkb import ollama_embeddings as ollama
from pkb.config import settings
from pkb.ingest import embedding_fingerprint


@pytest.fixture
def server(monkeypatch):
    calls = []

    def request(endpoint, path, *, payload=None, **kwargs):
        calls.append((path, payload))
        if path == "/api/tags":
            return {"models": [
                {"name": "embed:latest", "digest": ollama.VERIFIED_GEMMA2_DIGEST},
                {"name": "chat:latest", "digest": "def"},
            ]}
        if path == "/api/show":
            return {"capabilities": ["embedding"] if payload["model"] == "embed:latest" else ["completion"],
                    "model_info": {"general.architecture": "gemma-embedding2",
                                   "gemma-embedding2.embedding_length": 512,
                                   "gemma-embedding2.embedding_length_out": 768}}
        assert path == "/api/embed"
        return {"embeddings": [[1.0] + [0.0] * 767 for _ in payload["input"]]}

    monkeypatch.setattr(ollama, "request_json", request)
    return calls


def test_discovery_is_read_only_and_uses_output_dimension(server):
    rows = ollama.list_models("192.168.0.2")
    assert [row["name"] for row in rows] == ["embed:latest"]
    assert rows[0]["dimensions"] == 768
    assert rows[0]["canonical_id"] == "google/embeddinggemma-2"
    assert all(path != "/api/embed" for path, _ in server)
    info = ollama.inspect_model("192.168.0.2", "embed:latest")
    assert info["dimensions"] == 768 and info["endpoint"] == "http://192.168.0.2:11434"


def test_document_query_routes_use_prefixes_without_loading_local_model(server, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "google/embeddinggemma-2")
    monkeypatch.setattr(settings, "embedding_ollama_model", "embed:latest")
    monkeypatch.setattr(settings, "embedding_ollama_digest", ollama.VERIFIED_GEMMA2_DIGEST)
    monkeypatch.setattr(settings, "embedding_ollama_document_prefix", ollama.DOCUMENT_PREFIX)
    monkeypatch.setattr(settings, "embedding_ollama_query_prefix", ollama.QUERY_PREFIX)
    monkeypatch.setattr(settings, "embedding_dims", 768)
    monkeypatch.setattr(embeddings, "_embed", lambda *args: pytest.fail("local model must not be used"))
    embeddings.embed_documents(["문서"])
    embeddings.embed_queries(["질문"])
    bodies = [body for path, body in server if path == "/api/embed"]
    assert [body["input"] for body in bodies] == [["title: none | text: 문서"], ["task: search result | query: 질문"]]
    assert all(body["truncate"] is False and body["dimensions"] == 768 and "options" not in body for body in bodies)


@pytest.mark.parametrize("vectors", [[], [[1.0]], [[math.nan, 1.0]], [[0.0, 0.0]], [[True, 1.0]]])
def test_bad_vectors_fail_before_return(vectors):
    with pytest.raises(ValueError):
        ollama._vectors({"embeddings": vectors}, 1, 2)


def test_changed_model_is_not_silently_mixed(server):
    with pytest.raises(ValueError, match="변경"):
        ollama.embed("localhost", "embed:latest", ["문서"], 768, expected_digest="old")
    assert not any(path == "/api/embed" for path, _ in server)


def test_same_architecture_does_not_prove_media_vector_compatibility(server):
    info = ollama._model_info("http://localhost:11434", {"name": "embed:latest", "digest": "fine-tuned"})
    assert info["canonical_id"] == "ollama/embed:latest"
    assert info["document_prefix"] == ollama.DOCUMENT_PREFIX


def test_fingerprint_tracks_provider_digest_and_dimensions_not_address(monkeypatch):
    chunk = {"content": "문서"}
    local = embedding_fingerprint(chunk)
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "embedding_ollama_digest", "abc")
    remote = embedding_fingerprint(chunk)
    assert remote != local
    monkeypatch.setattr(settings, "embedding_ollama_endpoint", "http://other:11434")
    assert embedding_fingerprint(chunk) == remote
    monkeypatch.setattr(settings, "embedding_ollama_digest", "changed")
    assert embedding_fingerprint(chunk) != remote


@pytest.mark.parametrize("address", ["", "ftp://host", "http://user:secret@host", "http://bad host", "host:99999"])
def test_rejects_invalid_endpoints(address):
    with pytest.raises(ValueError):
        ollama.normalize_endpoint(address)
