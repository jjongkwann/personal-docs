"""Bounded attachment imports: preserve originals and index reviewable draft cards."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from contextlib import nullcontext
from pathlib import Path

import yaml

from pkb import access
from pkb.attachments import MAX_ATTACHMENT_BYTES, _resolve
from pkb.config import data_dir
from pkb.graph.status import document_status, status_record
from pkb.ingest import SUPPORTED_EXTENSIONS, ingest_files, parse_frontmatter, read_file_as_text
from pkb.operations import resolve_document_policy

MAX_IMPORT_BYTES = 50 * 1024 * 1024
_SEAL = re.compile(r"\n<!-- pkb-import-record-sha256: ([0-9a-f]{64}) -->\n\Z")


def _no_symlinks(path: Path) -> None:
    # Roots are resolved trusted configuration; reject symlinks in their children.
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError("심볼릭 링크를 포함한 경로는 가져올 수 없습니다.")


def _load_record(target: Path, attachment: str, expected: str) -> tuple[str, dict]:
    _no_symlinks(target)
    if not target.is_file():
        raise ValueError("가져오기 문서 경로가 일반 파일이 아닙니다.")
    with target.open("rb") as stream:
        raw = stream.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("기존 가져오기 문서가 크기 한도를 넘습니다.")
    content = raw.decode("utf-8")
    seal = _SEAL.search(content)
    if seal is None or hashlib.sha256(content[:seal.start()].encode()).hexdigest() != seal[1]:
        raise ValueError("기존 가져오기 문서가 수정되었습니다. 덮어쓰지 않습니다.")
    metadata, _ = parse_frontmatter(content)
    if (
        metadata.get("source_id") != f"sha256:{expected}"
        or metadata.get("attachments") != [attachment]
        or metadata.get("import_extraction") not in {"succeeded", "unsupported", "empty"}
        or type(metadata.get("import_chars")) is not int
        or not isinstance(metadata.get("title"), str)
    ):
        raise ValueError("기존 가져오기 문서의 원본 정보가 다릅니다. 덮어쓰지 않습니다.")
    return content, metadata


def _extract(source: Path, expected: str, *, convert: bool = True) -> tuple[str, str, int, str]:
    """Stream-hash originals; snapshot only supported files within the conversion limit."""
    digest = hashlib.sha256()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(source, flags), "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("일반 첨부 파일만 가져올 수 있습니다.")
        if info.st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("원본 첨부는 최대 2 GiB입니다.")
        supported = source.suffix.lower() in SUPPORTED_EXTENSIONS
        should_extract = convert and supported and info.st_size <= MAX_IMPORT_BYTES
        temporary = tempfile.TemporaryDirectory(prefix="pkb-import-") if should_extract else nullcontext(None)
        with temporary as directory:
            snapshot = Path(directory) / ("source" + source.suffix.lower()) if directory else None
            output = snapshot.open("wb") if snapshot else nullcontext(None)
            size = 0
            with output as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    size += len(block)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise ValueError("원본 첨부는 최대 2 GiB입니다.")
                    if writer and size > MAX_IMPORT_BYTES:
                        raise ValueError("변환 중 원본이 50 MiB 추출 한도를 넘었습니다. 다시 시도하세요.")
                    digest.update(block)
                    if writer:
                        writer.write(block)
            if digest.hexdigest() != expected:
                raise ValueError("원본 sha256이 일치하지 않습니다.")
            if not should_extract:
                note = ""
                if supported and info.st_size > MAX_IMPORT_BYTES:
                    note = "원본이 50 MiB 텍스트 추출 한도를 넘어 내용 추출을 생략했습니다."
                return "", "unsupported", size, note
            text = read_file_as_text(snapshot)
            if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
                raise ValueError("추출한 텍스트가 50 MiB 한도를 넘습니다.")
            return text, "succeeded" if text.strip() else "empty", size, ""


def import_attachment(
    path: str, sha256: str, category: str, title: str = "", *, document_id: str, version: int,
) -> dict:
    from pkb.import_versions import import_lock

    with import_lock():
        return _import_attachment(path, sha256, category, title, document_id=document_id, version=version)


def _import_attachment(
    path: str, sha256: str, category: str, title: str, *, document_id: str, version: int,
) -> dict:
    """Import a verified attachment into an existing category and synchronously index it.

    Drafts remain outside curated/evidence search. Unsupported and empty extraction
    still produce searchable metadata cards. Graph extraction runs in a separate worker.
    """
    from pkb.import_versions import validate_identity, validate_slot

    stage = "validation"
    doc_id = ""
    try:
        validate_identity(document_id, version)
        expected = sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("sha256은 64자리 16진수여야 합니다.")
        attachment_root, source, rel = _resolve(path)
        attachment = f"attachments/{rel}"
        access.require("read", attachment)
        access.require_file("read", "attachments/", attachment_root, source, label=attachment)
        _no_symlinks(source)
        if not source.is_file():
            raise ValueError("일반 첨부 파일이 없습니다.")
        if (
            not category or category in {".", "..", "concepts", "guides", "research", "daily-research"}
            or category.startswith((".", "_")) or any(char in category for char in "/\\\0")
        ):
            raise ValueError("기존 최상위 카테고리 이름 하나가 필요합니다.")
        root = data_dir()
        category_dir = root / category
        target = category_dir / "research" / f"import-{expected}.md"
        doc_id = f"data/{category}/research/{target.name}"
        access.require("write", doc_id)
        access.require_file("write", "data/", root, target, label=doc_id)
        _no_symlinks(target)
        if not category_dir.is_dir():
            raise ValueError("존재하는 카테고리를 지정하세요.")

        validate_slot(root, target, document_id, version)

        stage = "extraction"
        text, extraction, size, extraction_note = _extract(source, expected, convert=not target.exists())
        stage = "save"
        if target.exists():
            content, metadata = _load_record(target, attachment, expected)
        else:
            chosen_title = " ".join((title.strip() or source.name).splitlines())
            metadata = {
                "schema_version": 1, "title": chosen_title, "doc_type": "research",
                "canonical_id": f"attachment-import:{category}:{expected}",
                "status": "draft", "authority": "imported", "tags": ["attachment-import"],
                "source_type": "document", "source_id": f"sha256:{expected}",
                "source": attachment, "original_file": attachment, "attachments": [attachment],
                "import_extraction": extraction, "import_chars": len(text),
                "import_document_id": document_id, "import_version": version,
                "import_extraction_note": extraction_note,
            }
            body = (
                f"# {chosen_title}\n\n원본 첨부: {attachment}\n\n"
                f"SHA-256: {expected}\n\n크기: {size} bytes\n\n"
                "검토 상태: 미검토 초안. 그래프 반영 여부는 자료의 처리 상태에서 확인합니다.\n\n"
            )
            if extraction == "succeeded":
                body += "## 추출한 내용\n\n" + text + "\n"
            elif extraction == "unsupported":
                body += "내용 추출 미지원: 파일 메타데이터만 검색됩니다. OCR·음성 전사는 수행하지 않았습니다.\n"
                if extraction_note:
                    body += extraction_note + "\n"
            else:
                body += "추출한 텍스트가 비어 있습니다. 파일 메타데이터만 검색됩니다. OCR은 수행하지 않았습니다.\n"
            content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + body
            content += f"\n<!-- pkb-import-record-sha256: {hashlib.sha256(content.encode()).hexdigest()} -->\n"
            if len(content.encode()) > MAX_IMPORT_BYTES:
                raise ValueError("가져오기 문서가 50 MiB 한도를 넘습니다.")
            resolve_document_policy(doc_id, content, strict=True)
            target.parent.mkdir(exist_ok=True)
            _no_symlinks(target)
            access.require_file("write", "data/", root, target, label=doc_id)
            # Link a complete private temp file into place; concurrent creates cannot overwrite.
            fd, temporary = tempfile.mkstemp(prefix=".pkb-import-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    content, metadata = _load_record(target, attachment, expected)
            finally:
                os.unlink(temporary)

        if (metadata.get("import_document_id"), metadata.get("import_version")) != (document_id, version):
            raise ValueError("기존 가져오기 문서의 문서ID·버전이 다릅니다. 이전 문서는 명시적으로 마이그레이션하세요.")

        stage = "indexing"
        _no_symlinks(target)
        access.require_file("write", "data/", root, target, label=doc_id)
        resolve_document_policy(doc_id, content, strict=True)
        stats = ingest_files([target], base_dir=root, doc_id_prefix="data/")
        indexed = any(stats.get(key, 0) for key in ("added", "embedded", "reused", "moved", "metadata_updated"))
        if stats.get("files") != 1 or not indexed:
            raise ValueError("문서 청크 색인이 확인되지 않았습니다.")
        extraction = metadata["import_extraction"]
        message = {
            "succeeded": "추출한 내용과 메타데이터를 색인했습니다.",
            "unsupported": "내용 추출 미지원: 메타데이터만 색인했습니다. OCR·음성 전사는 수행하지 않았습니다.",
            "empty": "추출한 텍스트가 비어 있어 메타데이터만 색인했습니다. OCR은 수행하지 않았습니다.",
        }[extraction]
        if metadata.get("import_extraction_note"):
            message += " " + metadata["import_extraction_note"]
        if extraction != "succeeded":
            graph_status = status_record(doc_id, "not_applicable", 0, 0)
        else:
            try:
                graph_status = document_status(doc_id)
            except Exception:
                # A graph service outage must not undo a successful durable import.
                count = sum(stats.get(key, 0) for key in ("added", "embedded", "reused", "moved", "metadata_updated"))
                graph_status = status_record(doc_id, "waiting", count, count)
        return {
            "doc_id": doc_id, "document_id": document_id, "version": version,
            "attachment_path": rel, "sha256": expected,
            "title": metadata["title"], "extraction": extraction, "indexing": "succeeded",
            "graph": graph_status["status"], "graph_status": graph_status, "chars": metadata["import_chars"],
            "message": message + " 검토 전 초안으로 보관합니다.",
        }
    except Exception as exc:
        raise ValueError(f"{stage} 단계 실패{f' ({doc_id})' if doc_id else ''}: {exc}") from exc
