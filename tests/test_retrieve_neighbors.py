"""검색 결과 이웃 청크 확장은 hit 수와 무관하게 msearch 한 번만 사용한다."""

from pkb.retrieve import _attach_neighbors


class _FakeES:
    def __init__(self):
        self.calls = []

    def msearch(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "responses": [
                {
                    "hits": {
                        "hits": [
                            {"_source": {"chunk_index": 0, "section_path": "A", "content": "prev"}},
                            {"_source": {"chunk_index": 1, "section_path": "B", "content": "self"}},
                            {"_source": {"chunk_index": 2, "section_path": "C", "content": "next"}},
                        ]
                    }
                },
                {
                    "hits": {
                        "hits": [
                            {"_source": {"chunk_index": 4, "section_path": "D", "content": "prev2"}},
                            {"_source": {"chunk_index": 5, "section_path": "E", "content": "self2"}},
                        ]
                    }
                },
            ]
        }


def test_attach_neighbors_batches_queries_and_excludes_self():
    es = _FakeES()
    hits = [
        {"doc_id": "data/a.md", "chunk_index": 1},
        {"doc_id": "data/b.md", "chunk_index": 5},
        {"doc_id": "data/missing-index.md"},
    ]

    result = _attach_neighbors(es, hits, window=1)

    assert len(es.calls) == 1
    assert len(es.calls[0]["searches"]) == 4  # header/body 두 쌍
    assert [row["chunk_index"] for row in result[0]["neighbors"]] == [0, 2]
    assert [row["chunk_index"] for row in result[1]["neighbors"]] == [4]
    assert result[2]["neighbors"] == []


def test_attach_neighbors_empty_hits_skips_es():
    es = _FakeES()
    assert _attach_neighbors(es, []) == []
    assert es.calls == []
