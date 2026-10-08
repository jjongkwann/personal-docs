"""코퍼스 원본 파일의 목록과 바이트 조회. 색인 여부와 무관한 읽기 전용 경로."""

from __future__ import annotations

import hashlib
import mimetypes
import stat
from pathlib import Path

from pkb.config import data_dir

MAX_FILE_BYTES = 10 * 1024 * 1024


def _resolve(file_path: str) -> tuple[Path, Path]:
    root = data_dir()
    parts = file_path.rstrip("/").split("/")
    if parts[0] != "data" or any(
        not part or part.startswith(".") or "\\" in part or "\0" in part
        for part in parts[1:]
    ):
        raise ValueError("data/ 하위의 숨김 경로가 아닌 파일·폴더만 조회할 수 있습니다.")
    target = root
    for part in parts[1:]:
        target = target / part
        if target.is_symlink():
            raise ValueError("심볼릭 링크는 조회할 수 없습니다.")
    if not target.resolve().is_relative_to(root):
        raise ValueError("data/ 밖의 경로는 조회할 수 없습니다.")
    return root, target


def _mime_type(path: Path) -> str:
    mime_type, encoding = mimetypes.guess_type(path.name)
    return mime_type if mime_type and not encoding else "application/octet-stream"


def list_files(directory: str = "data", offset: int = 0, limit: int = 100) -> dict:
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset은 0 이상, limit은 1~200이어야 합니다.")
    root, target = _resolve(directory)
    if not target.is_dir():
        raise ValueError(f"폴더가 없습니다: {directory}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda path: path.name):
        if child.name.startswith(".") or "\\" in child.name or child.is_symlink():
            continue
        info = child.stat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            continue
        entry = {
            "file_path": "data/" + child.relative_to(root).as_posix(),
            "name": child.name,
            "type": "directory" if stat.S_ISDIR(info.st_mode) else "file",
        }
        if entry["type"] == "file":
            entry.update(size_bytes=info.st_size, mime_type=_mime_type(child))
        entries.append(entry)
    end = offset + limit
    return {
        "directory": "data" if target == root else "data/" + target.relative_to(root).as_posix(),
        "entries": entries[offset:end],
        "total": len(entries),
        "next_offset": end if end < len(entries) else None,
        "max_file_bytes": MAX_FILE_BYTES,
    }


def read_file_bytes(file_path: str) -> tuple[dict, bytes]:
    root, target = _resolve(file_path)
    if not target.is_file():
        raise ValueError(f"일반 파일이 아닙니다: {file_path}")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"파일은 최대 {MAX_FILE_BYTES}바이트(10 MiB)까지 받을 수 있습니다.")
    with target.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"파일은 최대 {MAX_FILE_BYTES}바이트(10 MiB)까지 받을 수 있습니다.")
    metadata = {
        "file_path": "data/" + target.relative_to(root).as_posix(),
        "name": target.name,
        "mime_type": _mime_type(target),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return metadata, data
