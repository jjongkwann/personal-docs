"""이미지·오디오 색인: 파일 목록·경로 경계·벡터 공간 검사·청크 구성·검색 필터·동기화."""

from __future__ import annotations

import base64
import hashlib
import os
from unittest.mock import MagicMock

import pytest

from pkb import media
from pkb.config import settings
from pkb.retrieve import hybrid_search, related_documents

OMNI = "jinaai/jina-embeddings-v5-omni-small"
TEXT = "jinaai/jina-embeddings-v5-text-small"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """볼트 안에 data 루트, 볼트 밖에 첨부 루트. 미디어 공간은 omni-small ↔ text-small."""
    vault = tmp_path / "vault"
    data = vault / "PKB"
    attachments = tmp_path / "attachments"
    for folder in (vault, data, attachments):
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "obsidian_path", str(vault))
    monkeypatch.setattr(settings, "data_root", str(data))
    monkeypatch.setattr(settings, "attachments_root", str(attachments))
    monkeypatch.setattr(settings, "embedding_model", TEXT)
    monkeypatch.setattr(settings, "embedding_task", "retrieval")
    monkeypatch.setattr(settings, "media_embedding_model", OMNI)
    monkeypatch.setattr(settings, "embedding_dims", 4)
    monkeypatch.setattr(media, "_sha_cache", {})
    monkeypatch.setattr(media, "_note_cache", {})
    return vault, data, attachments


def _write(path, data: bytes | str = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _es(stored: dict[str, tuple[str, str]] | None = None) -> MagicMock:
    """미디어 첫 청크 조회에 stored {doc_id: (sha, model)}를 돌려주는 ES."""
    es = MagicMock()
    hits = [
        {"_source": {"doc_id": doc_id, "media_sha256": sha, "media_model": model}, "sort": [doc_id]}
        for doc_id, (sha, model) in (stored or {}).items()
    ]
    es.search.return_value = {"hits": {"hits": hits}}
    return es


# ---------- 목록 ----------

def test_list_media_enumerates_three_roots_with_ingest_exclusions(roots):
    vault, data, attachments = roots
    _write(vault / "photos/cat.jpg")
    _write(vault / "assets/Diagram.SVG")
    _write(data / "study/chart.png")
    _write(attachments / "meetings/2026/m/others.m4a")
    _write(vault / "notes/a.md", "# a")  # Markdown은 미디어가 아니다
    for skipped in (
        vault / ".obsidian/icon.png", vault / "_trash/old.png", vault / "photos/cat.sync-conflict-20261015-1-A.jpg",
        attachments / ".uploads/x.part.m4a", attachments / ".hidden.m4a",
    ):
        _write(skipped)
    os.symlink(vault / "photos/cat.jpg", vault / "photos/link.jpg")

    listing = media.list_media(_es())

    assert listing["model"] == OMNI
    assert [(m["doc_id"], m["kind"]) for m in listing["media"]] == [
        ("data/study/chart.png", "image"),
        ("obsidian/assets/Diagram.SVG", "image"),
        ("obsidian/photos/cat.jpg", "image"),
        ("attachments/meetings/2026/m/others.m4a", "audio"),
    ]
    entry = listing["media"][0]
    assert entry["sha256"] == hashlib.sha256(b"x").hexdigest()
    assert entry["size_bytes"] == 1 and entry["indexed"] is False and entry["model"] is None
    assert entry["modified"].endswith("+00:00")


def test_indexed_means_same_sha_and_current_model(roots):
    vault, _, _ = roots
    _write(vault / "a.jpg", b"a")
    _write(vault / "b.jpg", b"b")
    _write(vault / "c.png", b"c")
    sha = {name: hashlib.sha256(name.encode()).hexdigest() for name in "abc"}
    es = _es({
        "obsidian/a.jpg": (sha["a"], OMNI),  # 그대로
        "obsidian/b.jpg": ("0" * 64, OMNI),  # 파일이 바뀜
        "obsidian/c.png": (sha["c"], "old/model"),  # 모델이 바뀜
    })

    listing = media.list_media(es)
    assert {m["doc_id"]: m["indexed"] for m in listing["media"]} == {
        "obsidian/a.jpg": True, "obsidian/b.jpg": False, "obsidian/c.png": False,
    }
    missing = media.list_media(es, missing_only=True, kinds=["image"], limit=1, offset=1)
    assert missing["total"] == 2
    assert [m["doc_id"] for m in missing["media"]] == ["obsidian/c.png"]


def test_list_media_reports_null_model_when_media_is_off_or_incompatible(roots, monkeypatch):
    vault, _, _ = roots
    _write(vault / "a.jpg")
    monkeypatch.setattr(settings, "embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "embedding_task", "")
    listing = media.list_media(_es({"obsidian/a.jpg": (hashlib.sha256(b"x").hexdigest(), OMNI)}))
    assert listing["model"] is None
    assert listing["media"][0]["indexed"] is False


def test_sha256_is_cached_by_size_and_mtime(roots):
    vault, _, _ = roots
    path = vault / "a.jpg"
    _write(path, b"one")
    first = media.file_sha256(path)
    stat = path.stat()
    path.write_bytes(b"two")  # 같은 크기
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert media.file_sha256(path) == first
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert media.file_sha256(path) == hashlib.sha256(b"two").hexdigest()


@pytest.mark.parametrize("kinds", [["video"], "image"])
def test_list_media_rejects_unknown_kinds(roots, kinds):
    with pytest.raises(ValueError, match="kinds"):
        media.list_media(_es(), kinds=kinds)


# ---------- 내려받기와 경로 경계 ----------

def test_get_media_round_trips_bytes_in_chunks(roots):
    _, _, attachments = roots
    payload = bytes(range(256)) * 10
    _write(attachments / "meetings/me.m4a", payload)

    received, offset = b"", 0
    while True:
        part = media.get_media("attachments/meetings/me.m4a", offset=offset, length=1000)
        received += base64.b64decode(part["data_base64"])
        offset += part["length"]
        if part["eof"]:
            break
    assert received == payload
    assert part["mime_type"] == "audio/mp4" and part["kind"] == "audio"
    assert part["sha256"] == hashlib.sha256(payload).hexdigest()
    assert part["doc_id"] == "attachments/meetings/me.m4a" and part["total_size"] == len(payload)


@pytest.mark.parametrize("doc_id", [
    "attachments/../secret.m4a",
    "attachments/.uploads/x.m4a",
    "obsidian/.obsidian/icon.png",
    "obsidian/_trash/old.png",
    "obsidian/photos/a.sync-conflict-20261015-1-A.jpg",
    "obsidian/PKB/chart.png",  # data 루트 안 파일은 data/ doc_id로만
    "obsidian/notes/a.md",
    "other/a.png",
    "/etc/hosts",
    "obsidian/photos/missing.jpg",
])
def test_get_media_refuses_paths_outside_the_media_set(roots, doc_id):
    vault, data, _ = roots
    _write(vault / "notes/a.md", "# a")
    _write(vault / ".obsidian/icon.png")
    _write(vault / "_trash/old.png")
    _write(data / "chart.png")
    with pytest.raises(ValueError):
        media.get_media(doc_id)


def test_get_media_refuses_symlinks(roots, tmp_path):
    vault, _, _ = roots
    _write(tmp_path / "outside.jpg")
    os.symlink(tmp_path / "outside.jpg", vault / "link.jpg")
    with pytest.raises(ValueError, match="심볼릭"):
        media.get_media("obsidian/link.jpg")


# ---------- 벡터 공간 ----------

def test_media_model_requires_setting_and_a_known_space(monkeypatch, roots):
    assert media.media_model() == OMNI
    monkeypatch.setattr(settings, "embedding_task", "text-matching")
    with pytest.raises(ValueError, match="같은 공간"):
        media.media_model()
    monkeypatch.setattr(settings, "embedding_task", "retrieval")
    monkeypatch.setattr(settings, "embedding_model", "BAAI/bge-m3")
    with pytest.raises(ValueError, match="같은 공간"):
        media.media_model()
    monkeypatch.setattr(settings, "media_embedding_model", "")
    with pytest.raises(ValueError, match="꺼져"):
        media.media_model()


# ---------- 벡터 받기 ----------

@pytest.fixture
def captured(monkeypatch):
    """store 쓰기를 가로챈다. existing은 지금 색인에 있는 청크."""
    calls: dict = {"existing": {}}
    monkeypatch.setattr("pkb.store.create_index", lambda es: None)
    monkeypatch.setattr("pkb.store.get_existing_chunks", lambda es, doc_id: calls["existing"])

    def apply(es, doc_id, new_chunks, metadata_updates, delete_indices):
        calls.update(doc_id=doc_id, chunks=new_chunks, deleted=delete_indices)

    monkeypatch.setattr("pkb.store.apply_chunk_delta", apply)
    monkeypatch.setattr("pkb.store.refresh_document_rows", lambda es, ids: calls.update(rows=list(ids)))
    return calls


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_put_audio_builds_one_chunk_per_window_with_note_context(roots, captured):
    vault, data, attachments = roots
    audio = attachments / "meetings/2026/w/others.m4a"
    _write(audio, b"audio")
    _write(data / "meetings/2026/w.md", (
        "---\ntitle: 주간 회의\nsource_type: meeting\noccurred_at: 2026-10-14T10:00:00+09:00\n"
        "people: [김민수]\nattachments: [meetings/2026/w/others.m4a]\n---\n# 주간 회의\n"
    ))
    captured["existing"] = {i: {"chunk_index": i} for i in range(4)}

    result = media.put_media_embeddings(
        MagicMock(), "attachments/meetings/2026/w/others.m4a", "audio", _sha(audio), OMNI,
        [{"start": 30, "end": 60, "vector": [0, 3, 0, 4]}, {"start": 0, "end": 30, "vector": [1, 0, 0, 0]}],
    )

    assert result == {"doc_id": "attachments/meetings/2026/w/others.m4a", "kind": "audio", "segments": 2,
                      "sha256": _sha(audio), "model": OMNI, "linked_from": ["data/meetings/2026/w.md"]}
    first, second = captured["chunks"]
    assert (first["media_start"], first["media_end"], first["section_path"]) == (0.0, 30.0, "0:00–0:30")
    assert second["embedding"] == pytest.approx([0, 0.6, 0, 0.8])  # 단위 길이로 맞춘다
    assert first["source_type"] == "audio" and first["category"] == "meetings" and first["title"] == "others"
    assert first["occurred_at"] == "2026-10-14T10:00:00+09:00" and first["people"] == ["김민수"]
    assert first["linked_from"] == ["data/meetings/2026/w.md"]
    assert "주간 회의" in first["content"] and first["content_hash"]
    assert first["media_sha256"] == _sha(audio) and first["media_model"] == OMNI
    assert captured["deleted"] == [2, 3]  # 남는 옛 창은 지운다
    assert captured["rows"] == ["attachments/meetings/2026/w/others.m4a"]


def test_put_image_takes_context_from_embedding_notes(roots, captured):
    vault, data, _ = roots
    image = vault / "photos/my cat.jpg"
    _write(image, b"img")
    _write(vault / "diary/d.md", "# 일기\n\n## 저녁\n고양이가 담요 위에서 잤다.\n![[my cat.jpg|300]]\n귀여웠다.\n")
    _write(data / "study/n.md", "# 노트\n![사진](../../photos/my%20cat.jpg)\n![외부](https://example.com/my%20cat.jpg)\n")
    _write(vault / "other.md", "# 무관\n![[dog.jpg]]\n")
    captured["existing"] = {0: {"chunk_index": 0, "archived_at": "2026-01-01T00:00:00+00:00"}}

    media.put_media_embeddings(MagicMock(), "obsidian/photos/my cat.jpg", "image", _sha(image), OMNI,
                               [{"vector": [1, 1, 1, 1]}])

    (chunk,) = captured["chunks"]
    assert chunk["linked_from"] == ["data/study/n.md", "obsidian/diary/d.md"]
    assert "고양이가 담요 위에서 잤다." in chunk["content"] and "저녁" in chunk["content"]
    assert "media_start" not in chunk and chunk["section_path"] == ""
    assert "occurred_at" not in chunk and "people" not in chunk
    assert chunk["archived_at"] == "2026-01-01T00:00:00+00:00"  # 아카이브는 유지
    assert captured["deleted"] == []


@pytest.mark.parametrize(("kind", "segments", "match"), [
    ("image", [{"vector": [1, 0, 0, 0]}] * 2, "하나만"),
    ("image", [{"vector": [1, 0, 0]}], "4차원"),
    ("image", [{"vector": [0, 0, 0, 0]}], "크기가 0"),
    ("image", [{"vector": [1, 0, 0, float("nan")]}], "유한"),
    ("image", [{"vector": [1, 0, 0, True]}], "유한"),
    ("image", [], "segments"),
    ("audio", [{"start": 0, "end": 31, "vector": [1, 0, 0, 0]}], "30초"),
    ("audio", [{"start": 5, "end": 5, "vector": [1, 0, 0, 0]}], "start < end"),
    ("audio", [{"start": 0, "end": 30, "vector": [1, 0, 0, 0]}, {"start": 20, "end": 40, "vector": [1, 0, 0, 0]}],
     "겹칩"),
    ("audio", [{"end": 30, "vector": [1, 0, 0, 0]}], "start"),
])
def test_put_validates_segments(roots, captured, kind, segments, match):
    vault, _, _ = roots
    path = vault / ("a.jpg" if kind == "image" else "a.m4a")
    _write(path)
    with pytest.raises(ValueError, match=match):
        media.put_media_embeddings(MagicMock(), f"obsidian/{path.name}", kind, _sha(path), OMNI, segments)
    assert "chunks" not in captured


def test_put_refuses_wrong_model_kind_hash_and_space(roots, captured, monkeypatch):
    vault, _, _ = roots
    _write(vault / "a.jpg")
    good = {"doc_id": "obsidian/a.jpg", "kind": "image", "sha256": _sha(vault / "a.jpg"), "model": OMNI,
            "segments": [{"vector": [1, 0, 0, 0]}]}
    for change, match in (({"model": "other"}, "model"), ({"kind": "audio"}, "kind"),
                          ({"sha256": "0" * 64}, "바뀌었")):
        with pytest.raises(ValueError, match=match):
            media.put_media_embeddings(MagicMock(), **{**good, **change})
    monkeypatch.setattr(settings, "embedding_model", "BAAI/bge-m3")
    with pytest.raises(ValueError, match="같은 공간"):
        media.put_media_embeddings(MagicMock(), **good)
    assert "chunks" not in captured


# ---------- 검색 ----------

def test_search_media_runs_knn_over_media_chunks_only(roots, monkeypatch):
    monkeypatch.setattr("pkb.embeddings.embed_queries", lambda texts: [[0.5, 0.5, 0.5, 0.5]])
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [{"_score": 0.7, "_source": {
        "doc_id": "attachments/m/others.m4a", "source_type": "audio", "title": "others", "media_start": 30.0,
        "media_end": 60.0, "content": "회의", "linked_from": ["data/m.md"],
    }}, {"_score": 0.6, "_source": {"doc_id": "obsidian/a.jpg", "source_type": "image", "title": "a",
                                     "content": "a"}}]}}

    out = media.search_media(es, "이사 후보지", kinds=["audio"], top_k=3, people=["김민수"], date_from="2026-10-01")

    knn = es.search.call_args.kwargs["knn"]
    assert knn["query_vector"] == [0.5, 0.5, 0.5, 0.5] and knn["k"] == 3
    assert {"term": {"media_model": OMNI}} in knn["filter"]
    assert {"terms": {"source_type": ["audio"]}} in knn["filter"]
    assert {"terms": {"people": ["김민수"]}} in knn["filter"]
    assert {"bool": {"must_not": {"exists": {"field": "archived_at"}}}} in knn["filter"]
    assert out["results"] == [
        {"doc_id": "attachments/m/others.m4a", "kind": "audio", "title": "others", "start": 30.0, "end": 60.0,
         "score": 0.7, "context": "회의", "linked_from": ["data/m.md"]},
        {"doc_id": "obsidian/a.jpg", "kind": "image", "title": "a", "start": None, "end": None,
         "score": 0.6, "context": "a", "linked_from": []},
    ]


def test_search_media_refuses_when_media_space_is_off(roots, monkeypatch):
    monkeypatch.setattr(settings, "media_embedding_model", "")
    with pytest.raises(ValueError, match="꺼져"):
        media.search_media(MagicMock(), "q")


# ---------- 텍스트 검색·목록은 기본으로 미디어를 뺀다 ----------

MEDIA_EXCLUDED = {"bool": {"must_not": {"terms": {"source_type": ["image", "audio"]}}}}


class _FakeES:
    def __init__(self):
        self.searches: list[list[dict]] = []

    def msearch(self, index, searches):
        self.searches.append(searches)
        return {"responses": [{"hits": {"hits": []}}, {"hits": {"hits": []}}]}


@pytest.mark.parametrize(("source_types", "excluded"), [(None, True), (["meeting"], True), (["Image"], False)])
def test_hybrid_search_excludes_media_unless_asked(monkeypatch, source_types, excluded):
    monkeypatch.setattr("pkb.retrieve.embed_queries", lambda texts: [[0.0] for _ in texts])
    es = _FakeES()
    hybrid_search(es, "q", log=False, source_types=source_types)
    (searches,) = es.searches
    assert (MEDIA_EXCLUDED in searches[1]["query"]["bool"]["filter"]) is excluded
    assert (MEDIA_EXCLUDED in searches[3]["knn"]["filter"]) is excluded


def test_related_documents_and_lists_exclude_media(monkeypatch):
    from pkb import store

    es = MagicMock()
    es.indices.exists.return_value = True
    es.get.return_value = {"_source": {"doc_vector": [1.0]}}
    es.search.return_value = {"hits": {"hits": []}}
    related_documents(es, "data/a.md")
    assert MEDIA_EXCLUDED in es.search.call_args.kwargs["knn"]["filter"]
    related_documents(es, "data/a.md", source_types=["audio"])
    assert MEDIA_EXCLUDED not in es.search.call_args.kwargs["knn"]["filter"]

    store.list_documents(es, include_archived=True)
    assert MEDIA_EXCLUDED in es.search.call_args.kwargs["query"]["bool"]["must"]


# ---------- 동기화 ----------

def test_reconcile_keeps_present_media_and_reports_vanished(roots, monkeypatch):
    from pkb import ingest

    _, data, _ = roots
    _write(data / "notes/a.md", "# a\n본문")
    _write(data / "notes/pic.png")
    monkeypatch.setattr(ingest, "ingest_files", lambda files, **kwargs: ingest._empty_stats())
    monkeypatch.setattr(ingest, "write_sync_marker", lambda ts=None: None)
    monkeypatch.setattr("pkb.store.list_doc_ids", lambda es, prefix: {
        "data/notes/a.md", "data/notes/pic.png", "data/notes/gone.png",
    })
    _, stale = ingest.reconcile(MagicMock(), data, "data/")
    assert stale == ["data/notes/gone.png"]
    assert ingest.find_ingestable_files(data) == [data / "notes/a.md"]  # 미디어는 인제스트하지 않는다


def test_stale_attachment_media(roots, monkeypatch):
    _, _, attachments = roots
    _write(attachments / "m/kept.m4a")
    monkeypatch.setattr("pkb.store.list_doc_ids", lambda es, prefix: {"attachments/m/kept.m4a",
                                                                      "attachments/m/gone.m4a"})
    assert media.stale_attachment_media(MagicMock()) == ["attachments/m/gone.m4a"]
    # 설정한 폴더가 없으면(볼륨 미연결) 아무것도 정리하지 않는다
    monkeypatch.setattr(settings, "attachments_root", str(attachments / "unmounted"))
    assert media.stale_attachment_media(MagicMock()) == []
    # 첨부 폴더를 끄면 남은 색인은 전부 정리 대상
    monkeypatch.setattr(settings, "attachments_root", "")
    assert media.stale_attachment_media(MagicMock()) == ["attachments/m/gone.m4a", "attachments/m/kept.m4a"]


def test_refresh_media_links_updates_only_changed_context(roots, monkeypatch):
    vault, data, attachments = roots
    _write(attachments / "m/a.m4a")
    _write(attachments / "m/b.m4a")
    _write(data / "m.md", "---\ntitle: 회의\npeople: [이지은]\nattachments: [m/a.m4a, m/b.m4a]\n---\n# 회의\n")
    links = media.link_index()
    current = media.media_links(media.resolve_media("attachments/m/a.m4a"), links).fields()
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [
        {"_source": {"doc_id": "attachments/m/a.m4a", **{k: current[k] for k in media._LINK_FIELDS}}},
        {"_source": {"doc_id": "attachments/m/b.m4a", "content_hash": "old", "linked_from": []}},
    ]}}
    rows: list = []
    monkeypatch.setattr("pkb.store.refresh_document_rows", lambda es, ids: rows.extend(ids))

    assert media.refresh_media_links(es) == 1

    call = es.update_by_query.call_args.kwargs
    assert call["query"] == {"term": {"doc_id": "attachments/m/b.m4a"}}
    params = call["script"]["params"]
    assert params["linked_from"] == ["data/m.md"] and params["people"] == ["이지은"]
    assert params["occurred_at"] is None  # 스크립트가 필드를 지운다
    assert rows == ["attachments/m/b.m4a"]


def test_media_chunks_never_feed_the_concept_graph(monkeypatch):
    from pkb.graph import services

    es = MagicMock()
    es.search.return_value = {"hits": {"hits": []}}
    monkeypatch.setattr(services.graph_store, "extracted_markers", lambda conn: ({}, {}))
    monkeypatch.setattr(services.graph_store, "extracted_input_markers", lambda conn: {})
    services.scan_pending_chunks(es, MagicMock())
    assert es.search.call_args.kwargs["query"]["bool"]["filter"] == [MEDIA_EXCLUDED]
