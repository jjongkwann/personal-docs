"""이미지·오디오 색인과 검색. 벡터는 서버가 아니라 클라이언트(`pkb media-sync`)가 만든다.

미디어는 세 루트에서 찾는다: data/(DATA_ROOT), obsidian/(OBSIDIAN_PATH, DATA_ROOT 제외),
attachments/(ATTACHMENTS_ROOT, 볼트 밖 회의 녹음 등). 제외 규칙은 Markdown 인제스트와 같다
(숨김 성분·예약 폴더·Syncthing 충돌 사본). 미디어 청크는 일반 청크와 같은 색인·embedding 필드에
source_type=image|audio로 들어가고, 오디오는 30초 창마다 청크 하나다. 텍스트 질의 벡터(embed_queries)와
같은 공간이어야 하므로 MEDIA_SPACES에 적힌 모델 쌍만 받는다.
"""

from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from pkb.config import data_dir, settings

MEDIA_KINDS = ("image", "audio")
IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".svg"}
)
AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".opus", ".aac"})
# 미디어 모델 → 질의를 만드는 텍스트 모델·어댑터. EmbeddingGemma 2는 글·이미지·오디오가 한 모델이다
# (서버는 EMBEDDING_TEXT_ONLY로 텍스트 탑만 올린다).
MEDIA_SPACES = {
    "google/embeddinggemma-2": ("google/embeddinggemma-2", ""),
}
# 오디오는 30초 창 하나가 벡터 하나다 — 검색 결과를 누르면 그 창의 처음부터 튼다
MAX_SEGMENT_SECONDS = 30.0
MAX_SEGMENTS = 1000  # 30초 창으로 8시간 남짓
_CONTEXT_CHARS = 2000
MEDIA_MAPPING_PROPERTIES = {
    "media_start": {"type": "float"},
    "media_end": {"type": "float"},
    "media_sha256": {"type": "keyword"},
    "media_model": {"type": "keyword"},
    "linked_from": {"type": "keyword"},
}
_MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".heic": "image/heic",
    ".avif": "image/avif",
    ".webp": "image/webp",
}


def media_kind(name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return None


def exclude_media_filter(source_types: list[str] | None) -> list[dict]:
    """source_types에 image·audio가 없으면 미디어 청크를 빼는 필터. 텍스트 검색·목록의 기본값."""
    if source_types and any(str(value).strip().lower() in MEDIA_KINDS for value in source_types):
        return []
    return [{"bool": {"must_not": {"terms": {"source_type": list(MEDIA_KINDS)}}}}]


def media_model() -> str:
    """서버가 받는 미디어 모델 id. 꺼져 있거나 EMBEDDING_MODEL과 공간이 다르면 ValueError."""
    model = settings.media_embedding_model.strip()
    if not model:
        raise ValueError("미디어 색인이 꺼져 있습니다 (MEDIA_EMBEDDING_MODEL이 비어 있음).")
    expected = MEDIA_SPACES.get(model)
    actual = (settings.embedding_model, settings.embedding_task)
    if expected != actual:
        raise ValueError(
            f"MEDIA_EMBEDDING_MODEL={model}의 벡터가 EMBEDDING_MODEL={actual[0]}"
            f"(task={actual[1] or '-'})의 질의 벡터와 같은 공간이 아닙니다. 맞는 쌍: {MEDIA_SPACES}"
        )
    return model


@dataclass(frozen=True)
class MediaFile:
    doc_id: str
    path: Path
    root: Path
    kind: str


def _nfc(text: str) -> str:
    from pkb.ingest import _nfc

    return _nfc(text)


def _roots() -> list[tuple[str, Path, tuple[Path, ...]]]:
    """(doc_id 접두사, 루트, 건너뛸 하위 트리). 다른 루트 안에 있는 루트는 그쪽 doc_id로만 센다."""
    data = data_dir()
    attachments = Path(settings.attachments_root).expanduser().resolve() if settings.attachments_root else None
    roots = [("data/", data, tuple(p for p in (attachments,) if p))]
    if settings.obsidian_path:
        vault = Path(settings.obsidian_path).expanduser().resolve()
        roots.append(("obsidian/", vault, tuple(p for p in (data, attachments) if p)))
    if attachments:
        roots.append(("attachments/", attachments, ()))
    return roots


def _excluded(rel: Path) -> bool:
    from pkb.ingest import is_excluded_path

    return is_excluded_path(rel)


def _walk(root: Path, skip: tuple[Path, ...], suffixes: frozenset[str]) -> Iterator[Path]:
    """root 아래 suffixes 파일. 숨김·예약 폴더, 충돌 사본, 심볼릭 링크, skip 하위 트리는 뺀다."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames
            if not _excluded(Path(name)) and (current / name) not in skip and not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            path = current / name
            if Path(name).suffix.lower() in suffixes and not _excluded(Path(name)) and not path.is_symlink():
                yield path


def iter_media_files(prefixes: tuple[str, ...] | None = None) -> Iterator[MediaFile]:
    for prefix, root, skip in _roots():
        if prefixes is not None and prefix not in prefixes:
            continue
        for path in _walk(root, skip, IMAGE_EXTENSIONS | AUDIO_EXTENSIONS):
            doc_id = prefix + _nfc(path.relative_to(root).as_posix())
            yield MediaFile(doc_id, path, root, media_kind(path.name) or "")


def media_doc_ids(root: Path, prefix: str, exclude: Path | None = None) -> set[str]:
    """root 아래 지금 있는 미디어 doc_id. 동기화가 미디어를 '원본에 없는 문서'로 보지 않게 한다."""
    skip = (exclude,) if exclude else ()
    return {prefix + _nfc(path.relative_to(root).as_posix())
            for path in _walk(root, skip, IMAGE_EXTENSIONS | AUDIO_EXTENSIONS)}


def resolve_media(doc_id: str) -> MediaFile:
    """list_media가 돌려줄 수 있는 doc_id만 실제 파일로 바꾼다. 경계 밖·숨김·링크·비미디어는 거부."""
    match = next((r for r in _roots() if isinstance(doc_id, str) and doc_id.startswith(r[0])), None)
    if match is None:
        raise ValueError(f"미디어 doc_id는 설정된 루트(data/, obsidian/, attachments/) 아래여야 합니다: {doc_id!r}")
    prefix, root, skip = match
    rel = doc_id[len(prefix):]
    parts = rel.split("/")
    if not rel or any(
        not part or part in {".", ".."} or "\\" in part or "\0" in part for part in parts
    ) or _excluded(Path(*parts)):
        raise ValueError(f"숨김·'..'·예약 폴더·충돌 사본 경로는 쓸 수 없습니다: {doc_id!r}")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"심볼릭 링크는 쓸 수 없습니다: {doc_id!r}")
    if not path.resolve().is_relative_to(root) or any(path.is_relative_to(other) for other in skip):
        raise ValueError(f"이 루트의 미디어가 아닙니다: {doc_id!r}")
    kind = media_kind(path.name)
    if kind is None:
        raise ValueError(f"이미지·오디오 파일이 아닙니다: {doc_id!r}")
    if not path.is_file():
        raise ValueError(f"파일이 없습니다: {doc_id}")
    return MediaFile(doc_id, path, root, kind)


# (크기, mtime_ns)가 같으면 다시 읽지 않는다 — list_media를 자주 불러도 해시는 바뀐 파일만 계산
_sha_cache: dict[Path, tuple[int, int, str]] = {}
_sha_lock = threading.Lock()


def file_sha256(path: Path) -> str:
    info = path.stat()
    key = (info.st_size, info.st_mtime_ns)
    with _sha_lock:
        cached = _sha_cache.get(path)
    if cached and cached[:2] == key:
        return cached[2]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    with _sha_lock:
        _sha_cache[path] = (*key, value)
    return value


def _mime_type(path: Path) -> str:
    mime_type = _MIME_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    return mime_type or "application/octet-stream"


def _kinds(kinds: list[str] | None) -> tuple[str, ...]:
    if not kinds:
        return MEDIA_KINDS
    if isinstance(kinds, str) or any(kind not in MEDIA_KINDS for kind in kinds):
        raise ValueError(f"kinds는 {list(MEDIA_KINDS)} 중에서 고른 목록이어야 합니다: {kinds!r}")
    return tuple(dict.fromkeys(kinds))


def _indexed_media(es) -> dict[str, dict]:
    """{doc_id: 첫 청크의 해시·모델·노트 맥락 필드} — 미디어 문서마다 하나씩."""
    from elasticsearch import NotFoundError

    rows: dict[str, dict] = {}
    search_after: list | None = None
    while True:
        kwargs = {
            "index": settings.es_index,
            "query": {"bool": {"filter": [
                {"terms": {"source_type": list(MEDIA_KINDS)}},
                {"term": {"chunk_index": 0}},
            ]}},
            "size": 1000,
            "source_includes": ["doc_id", "media_sha256", "media_model", *_LINK_FIELDS],
            "sort": [{"doc_id": "asc"}],
        }
        if search_after is not None:
            kwargs["search_after"] = search_after
        try:
            hits = es.search(**kwargs)["hits"]["hits"]
        except NotFoundError:
            return rows
        for hit in hits:
            rows[hit["_source"]["doc_id"]] = hit["_source"]
        if len(hits) < 1000:
            return rows
        search_after = hits[-1]["sort"]


def list_media(es, kinds: list[str] | None = None, missing_only: bool = False, limit: int = 200,
               offset: int = 0) -> dict:
    """세 루트의 이미지·오디오와 색인 상태. indexed는 지금 파일 해시와 지금 모델로 만든 청크가 있는지."""
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError("offset은 0 이상, limit은 1~1000이어야 합니다.")
    wanted = _kinds(kinds)
    try:
        model = media_model()
    except ValueError:
        model = None
    indexed = _indexed_media(es)
    entries = []
    for media in iter_media_files():
        if media.kind not in wanted:
            continue
        info = media.path.stat()
        sha = file_sha256(media.path)
        stored = indexed.get(media.doc_id, {})
        stored_sha, stored_model = stored.get("media_sha256"), stored.get("media_model")
        is_indexed = bool(model) and stored_sha == sha and stored_model == model
        if missing_only and is_indexed:
            continue
        entries.append({
            "doc_id": media.doc_id,
            "kind": media.kind,
            "size_bytes": info.st_size,
            "modified": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
            "sha256": sha,
            "indexed": is_indexed,
            "model": stored_model or None,
        })
    return {"total": len(entries), "model": model, "media": entries[offset : offset + limit]}


def get_media(doc_id: str, offset: int = 0, length: int | None = None) -> dict:
    """[offset, offset+length) 바이트를 base64로. get_attachment와 같은 모양에 doc_id·mime_type."""
    from pkb.attachments import MAX_CHUNK_BYTES

    length = MAX_CHUNK_BYTES if length is None else length
    media = resolve_media(doc_id)
    if not 1 <= length <= MAX_CHUNK_BYTES:
        raise ValueError(f"length는 1~{MAX_CHUNK_BYTES}바이트(4 MiB)여야 합니다.")
    total = media.path.stat().st_size
    if not 0 <= offset <= total:
        raise ValueError(f"offset은 0~{total}이어야 합니다.")
    with media.path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    result = {
        "doc_id": media.doc_id,
        "kind": media.kind,
        "mime_type": _mime_type(media.path),
        "offset": offset,
        "length": len(data),
        "total_size": total,
        "eof": offset + len(data) >= total,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    if result["eof"]:
        result["sha256"] = file_sha256(media.path)
    return result


# ---------- 노트 쪽 맥락: 이미지를 넣은 노트의 주변 줄, 녹음을 첨부로 단 노트 ----------

@dataclass(frozen=True)
class _Note:
    doc_id: str
    path: Path
    title: str
    occurred_at: str | None
    people: tuple[str, ...]
    attachments: tuple[str, ...]
    embeds: tuple[tuple[str, str], ...]  # (링크 대상 원문, 주변 줄)


_HEADING_RE = re.compile(r"^#{1,6}\s")
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_MD_EMBED_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)>]+?)>?(?:\s+\"[^\"]*\")?\s*\)")
_note_cache: dict[Path, tuple[int, int, _Note]] = {}
_note_lock = threading.Lock()


def _parse_note(path: Path, doc_id: str) -> _Note:
    from pkb.ingest import _extract_title, parse_frontmatter
    from pkb.items import normalize_item_metadata

    frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else _extract_title(body, path)
    item = normalize_item_metadata(frontmatter)
    lines = body.splitlines()
    embeds = []
    heading = ""
    for index, line in enumerate(lines):
        if _HEADING_RE.match(line):
            heading = line.lstrip("#").strip()
        targets = [m.group(1).split("|")[0].split("#")[0].strip() for m in _WIKI_EMBED_RE.finditer(line)]
        targets += [m.group(1).strip() for m in _MD_EMBED_RE.finditer(line)]
        if not targets:
            continue
        around = " ".join(
            text.strip() for text in lines[max(0, index - 2) : index + 3]
            if text.strip() and not text.strip().startswith("---")
        )
        context = f"{heading} — {around}" if heading else around
        embeds.extend((target, context[:500]) for target in targets if target)
    return _Note(
        doc_id=doc_id,
        path=path,
        title=_nfc(title),
        occurred_at=item.get("occurred_at"),
        people=tuple(item.get("people") or ()),
        attachments=tuple(item.get("attachments") or ()),
        embeds=tuple(embeds),
    )


def _notes() -> list[_Note]:
    """data/와 볼트의 Markdown 노트. 파일마다 (크기, mtime)가 같으면 전에 읽은 결과를 쓴다."""
    seen: set[Path] = set()
    notes = []
    for prefix, root, skip in _roots():
        if prefix == "attachments/":
            continue
        for path in _walk(root, skip, frozenset({".md", ".markdown"})):
            info = path.stat()
            key = (info.st_size, info.st_mtime_ns)
            with _note_lock:
                cached = _note_cache.get(path)
            if cached and cached[:2] == key:
                note = cached[2]
            else:
                note = _parse_note(path, prefix + _nfc(path.relative_to(root).as_posix()))
                with _note_lock:
                    _note_cache[path] = (*key, note)
            seen.add(path)
            notes.append(note)
    with _note_lock:
        for path in [p for p in _note_cache if p not in seen]:
            del _note_cache[path]
    return notes


@dataclass(frozen=True)
class MediaLinks:
    linked_from: tuple[str, ...]
    content: str
    occurred_at: str | None
    people: tuple[str, ...]

    def fields(self) -> dict:
        """청크에 쓰는 노트 맥락 필드. occurred_at·people이 없으면 None(청크에서 뺀다)."""
        from pkb.ingest import _content_hash, _detect_language

        return {
            "content": self.content,
            "content_hash": _content_hash(self.content),
            "language": _detect_language(self.content),
            "linked_from": list(self.linked_from),
            "occurred_at": self.occurred_at,
            "people": list(self.people) or None,
        }


_LINK_FIELDS = ("content_hash", "linked_from", "occurred_at", "people")


def _resolve_embed(note: _Note, target: str, by_path: dict[Path, str], by_name: dict[str, list[str]]) -> list[str]:
    """노트 안 임베드 대상을 미디어 doc_id로. 위키링크는 이름만 있으면 볼트 전체에서 이름으로 찾는다."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):  # http:, data: 등 외부 자원
        return []
    target = unquote(target)
    candidates = [note.path.parent / target]
    candidates += [root / target for prefix, root, _ in _roots() if prefix != "attachments/"]
    for candidate in candidates:
        doc_id = by_path.get(Path(os.path.normpath(candidate)))
        if doc_id:
            return [doc_id]
    # 같은 이름이 여럿이면 Obsidian도 모호하다 — 모두에 연결한다
    return [] if "/" in target else by_name.get(_nfc(target), [])


def link_index() -> dict[str, list[tuple[_Note, str]]]:
    """{미디어 doc_id: [(가리키는 노트, 주변 줄)]}. 노트·미디어를 한 번씩만 훑는다.

    볼트·data/ 노트의 ![[이름]]·![](경로) 임베드와 frontmatter attachments(첨부 폴더 기준 경로)를 본다.
    """
    by_path: dict[Path, str] = {}
    by_name: dict[str, list[str]] = {}
    for other in iter_media_files(("data/", "obsidian/")):
        by_path[Path(os.path.normpath(other.path))] = other.doc_id
        by_name.setdefault(_nfc(other.path.name), []).append(other.doc_id)

    index: dict[str, list[tuple[_Note, str]]] = {}
    for note in sorted(_notes(), key=lambda n: n.doc_id):
        contexts: dict[str, list[str]] = {}
        for path in note.attachments:
            contexts.setdefault("attachments/" + _nfc(path.strip("/")), [])
        for target, context in note.embeds:
            for doc_id in _resolve_embed(note, target, by_path, by_name):
                contexts.setdefault(doc_id, []).append(context)
        for doc_id, found in contexts.items():
            index.setdefault(doc_id, []).append((note, " / ".join(dict.fromkeys(found))))
    return index


def media_links(media: MediaFile, index: dict[str, list[tuple[_Note, str]]] | None = None) -> MediaLinks:
    """미디어를 가리키는 노트와, 검색 결과에 보여 줄 설명 글(content)."""
    linked = (link_index() if index is None else index).get(media.doc_id, [])
    lines = [_nfc(media.path.stem), media.doc_id]
    for note, context in linked:
        details = [note.title, note.doc_id]
        if note.occurred_at:
            details.append(note.occurred_at)
        if note.people:
            details.append(", ".join(note.people))
        lines.append(" | ".join(details))
        if context:
            lines.append(context)
    occurred_at = next((note.occurred_at for note, _ in linked if note.occurred_at), None)
    people = tuple(dict.fromkeys(person for note, _ in linked for person in note.people))
    return MediaLinks(
        linked_from=tuple(note.doc_id for note, _ in linked),
        content="\n".join(lines)[:_CONTEXT_CHARS],
        occurred_at=occurred_at,
        people=people,
    )


# ---------- 벡터 받기 ----------

def _unit_vector(values: object) -> list[float]:
    if not isinstance(values, list) or len(values) != settings.embedding_dims:
        raise ValueError(f"vector는 {settings.embedding_dims}차원 숫자 목록이어야 합니다.")
    floats = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("vector에 유한한 숫자가 아닌 값이 있습니다.")
        floats.append(float(value))
    norm = math.sqrt(sum(value * value for value in floats))
    if norm == 0:
        raise ValueError("vector의 크기가 0입니다.")
    return [value / norm for value in floats]


def _seconds(segment: dict, key: str) -> float:
    value = segment.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"segment {key}는 유한한 초 단위 숫자여야 합니다: {value!r}")
    return float(value)


def _clock(seconds: float) -> str:
    whole = int(seconds)
    if whole >= 3600:
        return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
    return f"{whole // 60}:{whole % 60:02d}"


def _segments(kind: str, segments: list[dict]) -> list[tuple[float | None, float | None, list[float]]]:
    """(start, end, 단위 벡터). 이미지는 하나, 오디오는 겹치지 않는 30초 이하 창을 시작 순으로."""
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError(f"segments는 1~{MAX_SEGMENTS}개여야 합니다.")
    if any(not isinstance(segment, dict) for segment in segments):
        raise ValueError("segments의 항목은 {start, end, vector} 객체여야 합니다.")
    if kind == "image":
        if len(segments) != 1:
            raise ValueError("이미지는 segment 하나만 받습니다.")
        return [(None, None, _unit_vector(segments[0].get("vector")))]
    parsed = sorted(
        ((_seconds(s, "start"), _seconds(s, "end"), _unit_vector(s.get("vector"))) for s in segments),
        key=lambda item: item[0],
    )
    previous_end = 0.0
    for start, end, _ in parsed:
        if not 0 <= start < end or end - start > MAX_SEGMENT_SECONDS + 1e-6:
            raise ValueError(
                f"오디오 segment는 0 ≤ start < end, 길이 {MAX_SEGMENT_SECONDS:g}초 이하여야 합니다: {start}~{end}"
            )
        if start < previous_end - 1e-6:
            raise ValueError(f"오디오 segment가 겹칩니다: {start} < {previous_end}")
        previous_end = end
    return parsed


def put_media_embeddings(es, doc_id: str, kind: str, sha256: str, model: str, segments: list[dict]) -> dict:
    """doc_id의 미디어 청크를 통째로 바꾼다. 파일 해시·모델·공간·차원이 맞을 때만."""
    from pkb.ingest import _extract_category
    from pkb.search_log import log_change
    from pkb.store import apply_chunk_delta, create_index, get_existing_chunks, refresh_document_rows

    accepted = media_model()
    if model != accepted:
        raise ValueError(f"model이 서버의 MEDIA_EMBEDDING_MODEL과 다릅니다: 받은 {model!r}, 서버 {accepted!r}")
    media = resolve_media(doc_id)
    if kind != media.kind:
        raise ValueError(f"kind가 파일 종류와 다릅니다: 받은 {kind!r}, 파일 {media.kind!r}")
    actual = file_sha256(media.path)
    if sha256.strip().lower() != actual:
        raise ValueError(f"파일이 바뀌었습니다: 받은 sha256 {sha256}, 지금 {actual}. list_media부터 다시 하세요.")
    parsed = _segments(kind, segments)

    links = media_links(media)
    mtime = datetime.fromtimestamp(media.path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d")
    base: dict = {
        "source_path": media.doc_id,
        "category": _extract_category(media.path, media.root),
        "doc_id": media.doc_id,
        "title": _nfc(media.path.stem),
        "tags": [],
        "date_modified": mtime,
        "source_type": kind,
        "media_sha256": actual,
        "media_model": accepted,
        **{key: value for key, value in links.fields().items() if value is not None},
    }

    create_index(es)  # 색인이 없으면 bulk가 동적 매핑으로 만들어 embedding이 dense_vector가 아니게 된다
    existing = get_existing_chunks(es, media.doc_id)
    first = existing.get(0, {})
    for field in ("archived_at", "archive_reason"):  # 아카이브는 미디어를 다시 넣어도 유지
        if first.get(field):
            base[field] = first[field]
    chunks = []
    for index, (start, end, vector) in enumerate(parsed):
        chunk = {**base, "chunk_index": index, "embedding": vector, "section_path": ""}
        if start is not None:
            chunk.update(media_start=start, media_end=end, section_path=f"{_clock(start)}–{_clock(end)}")
        chunks.append(chunk)
    apply_chunk_delta(es, media.doc_id, chunks, [], sorted(set(existing) - set(range(len(chunks)))))
    refresh_document_rows(es, [media.doc_id])
    log_change("media", media.doc_id, chunks=len(chunks), model=accepted)
    return {"doc_id": media.doc_id, "kind": kind, "segments": len(chunks), "sha256": actual, "model": accepted,
            "linked_from": list(links.linked_from)}


# ---------- 검색 ----------

def search_media(
    es,
    query: str,
    kinds: list[str] | None = None,
    top_k: int = 12,
    include_archived: bool = False,
    doc_ids: list[str] | None = None,
    people: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    path_prefix: str | None = None,
) -> dict:
    """텍스트 질의로 미디어 청크만 kNN. 오디오는 30초 창 단위로 여러 개가 나올 수 있다."""
    from pkb.embeddings import embed_queries
    from pkb.retrieve import _lifecycle_filter, scope_filter

    model = media_model()
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query가 비어 있습니다.")
    if not 1 <= top_k <= 100:
        raise ValueError("top_k는 1~100이어야 합니다.")
    filters = [
        {"term": {"media_model": model}},
        *_lifecycle_filter(include_archived),
        *scope_filter(
            doc_ids=doc_ids, source_types=list(_kinds(kinds)), people=people,
            date_from=date_from, date_to=date_to, path_prefix=path_prefix,
        ),
    ]
    vector = embed_queries([query.strip()])[0]
    result = es.search(
        index=settings.es_index,
        knn={
            "field": "embedding",
            "query_vector": vector,
            "k": top_k,
            "num_candidates": max(top_k * 10, 100),
            "filter": filters,
        },
        size=top_k,
        source_excludes=["embedding"],
    )
    results = []
    for hit in result["hits"]["hits"]:
        source = hit["_source"]
        results.append({
            "doc_id": source["doc_id"],
            "kind": source.get("source_type"),
            "title": source.get("title"),
            "start": source.get("media_start"),
            "end": source.get("media_end"),
            "score": hit["_score"],
            "context": (source.get("content") or "")[:500],
            "linked_from": source.get("linked_from") or [],
        })
    return {"query": query, "results": results}


# ---------- 동기화 ----------

# 값이 None이면 필드를 지운다 — 노트에서 occurred_at·people을 뺀 경우
_REFRESH_SCRIPT = (
    "for (entry in params.entrySet()) { if (entry.getValue() == null) { ctx._source.remove(entry.getKey()) }"
    " else { ctx._source[entry.getKey()] = entry.getValue() } }"
)


def refresh_media_links(es) -> int:
    """색인된 미디어의 노트 맥락(content·linked_from·occurred_at·people)을 지금 노트에 맞춘다.

    벡터는 그대로 두고 바뀐 문서만 고친다. 노트를 옮기거나 제목·people·occurred_at을 고쳐도
    search_media의 linked_from과 필터가 따라온다. 파일이 사라진 미디어는 동기화의 정리 대상이라
    건드리지 않는다. 고친 문서 수를 반환한다.
    """
    from pkb.search_log import log_change
    from pkb.store import refresh_document_rows

    indexed = _indexed_media(es)
    if not indexed:
        return 0
    files = {media.doc_id: media for media in iter_media_files() if media.doc_id in indexed}
    index = link_index()
    changed = []
    for doc_id, media in files.items():
        fields = media_links(media, index).fields()
        stored = indexed[doc_id]
        if all(stored.get(key) == fields[key] for key in _LINK_FIELDS):
            continue
        es.update_by_query(
            index=settings.es_index,
            query={"term": {"doc_id": doc_id}},
            script={"source": _REFRESH_SCRIPT, "lang": "painless", "params": fields},
            refresh=True,
        )
        log_change("media-links", doc_id, linked_from=fields["linked_from"])
        changed.append(doc_id)
    refresh_document_rows(es, changed)
    return len(changed)


def stale_attachment_media(es) -> list[str]:
    """색인에는 있는데 첨부 폴더에서 사라진 미디어 doc_id. 첨부 폴더를 끄면 전부.

    설정한 폴더가 없으면(외장 볼륨이 빠졌을 때 등) 설정 오류로 보고 아무것도 정리하지 않는다.
    """
    from pkb.store import list_doc_ids

    if settings.attachments_root and not Path(settings.attachments_root).expanduser().is_dir():
        return []

    present = {media.doc_id for media in iter_media_files(("attachments/",))}
    return sorted(list_doc_ids(es, "attachments/") - present)
