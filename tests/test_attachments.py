"""볼트 밖 첨부의 조각 업로드·다운로드와 경로 경계."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from pkb import attachments, mcp_server

PAYLOAD = bytes(range(256)) * 40  # 10 KiB
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr("pkb.config.settings.attachments_root", str(root))
    return root


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _upload(path: str, data: bytes, size: int, sha: str = SHA) -> dict:
    result = {}
    for offset in range(0, len(data), size):
        piece = data[offset : offset + size]
        last = offset + len(piece) == len(data)
        result = attachments.put_attachment(path, _b64(piece), offset, len(data), sha if last else "")
    return result


def test_chunked_upload_moves_file_into_place_when_hash_matches(root):
    result = _upload("meetings/2026/standup.m4a", PAYLOAD, 4096)

    assert result == {"path": "meetings/2026/standup.m4a", "received": len(PAYLOAD),
                      "total_size": len(PAYLOAD), "complete": True, "sha256": SHA}
    assert (root / "meetings/2026/standup.m4a").read_bytes() == PAYLOAD
    assert list((root / ".uploads").iterdir()) == []


def test_partial_upload_is_invisible_and_resumes_from_received_bytes(root):
    first = attachments.put_attachment("a.bin", _b64(PAYLOAD[:100]), 0, len(PAYLOAD))
    assert first == {"path": "a.bin", "received": 100, "total_size": len(PAYLOAD), "complete": False}
    assert not (root / "a.bin").exists()

    with pytest.raises(ValueError, match="받은 바이트 100부터"):
        attachments.put_attachment("a.bin", _b64(PAYLOAD[200:300]), 200, len(PAYLOAD))
    done = attachments.put_attachment("a.bin", _b64(PAYLOAD[100:]), 100, len(PAYLOAD), SHA)
    assert done["complete"] is True
    assert (root / "a.bin").read_bytes() == PAYLOAD


def test_offset_zero_restarts_the_upload(root):
    attachments.put_attachment("a.bin", _b64(b"garbage"), 0, len(PAYLOAD))
    assert _upload("a.bin", PAYLOAD, len(PAYLOAD))["complete"] is True
    assert (root / "a.bin").read_bytes() == PAYLOAD


def test_hash_mismatch_discards_the_upload(root):
    with pytest.raises(ValueError, match="sha256이 맞지 않아"):
        attachments.put_attachment("a.bin", _b64(PAYLOAD), 0, len(PAYLOAD), "0" * 64)
    assert not (root / "a.bin").exists()
    assert list((root / ".uploads").iterdir()) == []


def test_final_chunk_requires_hash_before_writing(root):
    attachments.put_attachment("a.bin", _b64(PAYLOAD[:10]), 0, len(PAYLOAD))
    with pytest.raises(ValueError, match="sha256이 필요"):
        attachments.put_attachment("a.bin", _b64(PAYLOAD[10:]), 10, len(PAYLOAD))
    # 거부된 조각은 쓰지 않았으므로 같은 조각을 다시 보낼 수 있다
    assert attachments.put_attachment("a.bin", _b64(PAYLOAD[10:]), 10, len(PAYLOAD), SHA)["complete"]


def test_existing_file_is_never_overwritten_but_a_retry_with_same_hash_succeeds(root):
    _upload("a.bin", PAYLOAD, len(PAYLOAD))
    retry = attachments.put_attachment("a.bin", _b64(PAYLOAD), 0, len(PAYLOAD), SHA)
    assert retry["complete"] is True
    other = b"different"
    with pytest.raises(ValueError, match="덮어쓰지 않음"):
        attachments.put_attachment("a.bin", _b64(other), 0, len(other), hashlib.sha256(other).hexdigest())
    assert (root / "a.bin").read_bytes() == PAYLOAD


@pytest.mark.parametrize("path", [
    "", "/etc/passwd", "../outside", "a/../../outside", "a//b", ".uploads/x.part", "a/.hidden", "a\\b", "a\0b",
])
def test_rejects_paths_outside_or_hidden(root, path):
    with pytest.raises(ValueError):
        attachments.put_attachment(path, _b64(b"x"), 0, 1, hashlib.sha256(b"x").hexdigest())
    with pytest.raises(ValueError):
        attachments.get_attachment(path)


def test_rejects_symlink_escaping_the_root(root, tmp_path):
    (tmp_path / "outside").mkdir()
    (root / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="밖"):
        attachments.put_attachment("link/x.bin", _b64(b"x"), 0, 1, hashlib.sha256(b"x").hexdigest())


def test_chunk_and_total_size_limits(root, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_CHUNK_BYTES", 8)
    with pytest.raises(ValueError, match="조각은 최대"):
        attachments.put_attachment("a.bin", _b64(b"123456789"), 0, 9)
    with pytest.raises(ValueError, match="total_size"):
        attachments.put_attachment("a.bin", _b64(b"x"), 0, attachments.MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ValueError, match="base64"):
        attachments.put_attachment("a.bin", "not base64!", 0, 3)


def test_disabled_without_attachments_root(monkeypatch):
    monkeypatch.setattr("pkb.config.settings.attachments_root", "")
    with pytest.raises(ValueError, match="ATTACHMENTS_ROOT"):
        attachments.get_attachment("a.bin")


def test_download_in_chunks_with_hash_on_the_last_one(root):
    _upload("a.bin", PAYLOAD, len(PAYLOAD))
    received = b""
    offset = 0
    while True:
        part = attachments.get_attachment("a.bin", offset, 4096)
        received += base64.b64decode(part["data_base64"])
        offset += part["length"]
        if part["eof"]:
            break
        assert "sha256" not in part
    assert received == PAYLOAD
    assert part["sha256"] == SHA and part["total_size"] == len(PAYLOAD)


def test_mcp_tools_round_trip_and_report_errors(root):
    put = getattr(mcp_server.put_attachment, "fn", mcp_server.put_attachment)
    assert json.loads(put("x/a.bin", _b64(PAYLOAD), 0, len(PAYLOAD), SHA))["complete"] is True
    assert put("../a.bin", _b64(b"x"), 0, 1, "").startswith("오류: ValueError")

    result = mcp_server.get_attachment("x/a.bin")
    assert result.structured_content is None  # base64가 두 번 실리지 않게
    payload = json.loads(result.content[0].text)
    assert base64.b64decode(payload["data_base64"]) == PAYLOAD and payload["eof"] is True

    missing = mcp_server.get_attachment("x/none.bin")
    assert missing.is_error is True
    assert missing.content[0].text.startswith("오류:")
