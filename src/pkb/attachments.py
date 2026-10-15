"""볼트 밖 첨부 폴더의 조각 단위 업로드·다운로드. 색인하지 않는다.

JSON-RPC 게이트웨이를 지나므로 한 번에 MAX_CHUNK_BYTES까지만 주고받는다(base64로 약 1.33배).
업로드는 숨김 폴더의 임시 파일에 이어 쓰고, 마지막 조각에서 SHA-256이 맞으면 제자리로
원자적으로 옮긴다. 같은 경로에 이미 파일이 있으면 덮어쓰지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import threading
from pathlib import Path

from pkb.config import settings

MAX_CHUNK_BYTES = 4 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024 * 1024
_UPLOAD_DIR = ".uploads"
# 같은 경로에 두 업로드가 동시에 이어 쓰지 않도록 put_attachment 전체를 직렬화한다.
_upload_lock = threading.Lock()


def _root() -> Path:
    if not settings.attachments_root:
        raise ValueError("첨부 폴더가 설정되지 않았습니다 (ATTACHMENTS_ROOT).")
    return Path(settings.attachments_root).expanduser().resolve()


def _resolve(path: str) -> tuple[Path, Path, str]:
    """(루트, 대상 절대경로, 정규화한 상대경로). 절대경로·'..'·숨김 성분·루트 밖은 거부."""
    root = _root()
    parts = path.split("/") if isinstance(path, str) else []
    if (
        not path
        or path.startswith("/")
        or any(not part or part in {".", ".."} or part.startswith(".") or "\\" in part or "\0" in part
               for part in parts)
    ):
        raise ValueError(f"첨부 폴더 아래 상대경로만 쓸 수 있습니다 (숨김·'..' 불가): {path!r}")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"첨부 폴더 밖을 가리킵니다: {path!r}")
    return root, target, "/".join(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def put_attachment(path: str, data_base64: str, offset: int, total_size: int, sha256: str = "") -> dict:
    """조각 하나를 받는다. offset은 지금까지 받은 바이트 수와 같아야 하고, 0이면 처음부터 다시 받는다.

    마지막 조각(offset + 조각 길이 == total_size)에는 전체 파일의 sha256이 필요하다.
    반환: path, received, total_size, complete (완료면 sha256도).
    """
    root, target, rel = _resolve(path)
    if not 0 <= total_size <= MAX_ATTACHMENT_BYTES:
        raise ValueError(f"total_size는 0~{MAX_ATTACHMENT_BYTES}바이트여야 합니다.")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data_base64가 올바른 base64가 아닙니다.") from None
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError(f"조각은 최대 {MAX_CHUNK_BYTES}바이트(4 MiB)입니다.")
    if offset < 0 or offset + len(data) > total_size:
        raise ValueError(f"offset({offset})+조각({len(data)})이 total_size({total_size})를 넘습니다.")
    expected = sha256.strip().lower()
    if offset + len(data) == total_size and not expected:
        raise ValueError("마지막 조각에는 전체 파일의 sha256이 필요합니다.")

    with _upload_lock:
        if target.exists():
            existing = _sha256(target) if target.is_file() else ""
            if expected and existing == expected:
                return {"path": rel, "received": total_size, "total_size": total_size,
                        "complete": True, "sha256": existing}
            raise ValueError(f"이미 있는 첨부입니다 (덮어쓰지 않음): {rel} sha256={existing or '<폴더>'}")

        partial = root / _UPLOAD_DIR / (hashlib.sha256(rel.encode("utf-8")).hexdigest() + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        received = partial.stat().st_size if partial.exists() and offset else 0
        if offset != received:
            raise ValueError(f"offset이 맞지 않습니다: 받은 바이트 {received}부터 이어 보내세요.")
        with partial.open("ab" if offset else "wb") as stream:
            stream.write(data)
        received += len(data)
        if received < total_size:
            return {"path": rel, "received": received, "total_size": total_size, "complete": False}

        actual = _sha256(partial)
        if actual != expected:
            partial.unlink()
            raise ValueError(f"sha256이 맞지 않아 버렸습니다: 받은 {actual}, 기대 {expected}. 처음부터 다시 보내세요.")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, target)
    return {"path": rel, "received": received, "total_size": total_size, "complete": True, "sha256": actual}


def get_attachment(path: str, offset: int = 0, length: int = MAX_CHUNK_BYTES) -> dict:
    """[offset, offset+length) 바이트를 base64로. 끝에 닿은 조각에는 전체 sha256을 붙인다."""
    _, target, rel = _resolve(path)
    if not target.is_file():
        raise ValueError(f"첨부가 없습니다: {rel}")
    if not 1 <= length <= MAX_CHUNK_BYTES:
        raise ValueError(f"length는 1~{MAX_CHUNK_BYTES}바이트(4 MiB)여야 합니다.")
    total = target.stat().st_size
    if not 0 <= offset <= total:
        raise ValueError(f"offset은 0~{total}이어야 합니다.")
    with target.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    result = {
        "path": rel,
        "offset": offset,
        "length": len(data),
        "total_size": total,
        "eof": offset + len(data) >= total,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    if result["eof"]:
        result["sha256"] = _sha256(target)
    return result
