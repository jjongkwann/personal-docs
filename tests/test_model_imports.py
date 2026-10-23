"""Remote embedding paths must not import the unused local ML runtime."""

import os
import subprocess
import sys
import textwrap


def test_remote_embeddings_and_server_imports_do_not_load_local_ml(tmp_path):
    script = textwrap.dedent("""\
        import importlib.abc
        import sys

        local_ml = {"torch", "transformers", "sentence_transformers"}

        class NoLocalModels(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in local_ml:
                    raise AssertionError(f"Unused local ML import: {fullname}")

        sys.meta_path.insert(0, NoLocalModels())
        from pkb import embeddings, mcp_server, ollama_embeddings, rerank, retrieve
        from pkb.config import settings
        from pkb.graph import worker

        settings.embedding_dims = 2
        settings.embedding_ollama_model = "remote:latest"
        settings.embedding_ollama_digest = "digest"
        settings.embedding_ollama_document_prefix = "document: "
        settings.embedding_ollama_query_prefix = "query: "
        inputs = []

        def request(endpoint, path, *, payload=None, **kwargs):
            if path == "/api/tags":
                return {"models": [{"name": "remote:latest", "digest": "digest"}]}
            assert path == "/api/embed"
            inputs.extend(payload["input"])
            return {"embeddings": [[1.0, 0.0] for _ in payload["input"]]}

        ollama_embeddings.request_json = request
        assert embeddings.embed_documents(["doc"]) == [[1.0, 0.0]]
        assert embeddings.embed_queries(["query"]) == [[1.0, 0.0]]
        assert inputs == ["document: doc", "query: query"]
        assert rerank.rerank("query", []) == []
        assert not embeddings._model.loaded
        assert not rerank._reranker.loaded
        assert local_ml.isdisjoint(sys.modules)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "DATA_ROOT": str(tmp_path), "GRAPH_DB_PATH": str(tmp_path / "graph.sqlite"),
             "EMBEDDING_PROVIDER": "ollama"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
