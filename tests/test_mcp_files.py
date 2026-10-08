"""원격 클라이언트의 원본 파일 조회·전송과 코퍼스 경계 검증."""

from __future__ import annotations

import base64
import hashlib
import os

import pytest
from starlette.testclient import TestClient

from pkb import files, mcp_server


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setattr("pkb.config.settings.data_root", str(root))
    return root


def test_list_files_includes_unindexed_originals_and_paginates(corpus):
    origin = corpus / "about/_origin/brand"
    origin.mkdir(parents=True)
    (origin / "a.svg").write_bytes(b"<svg/>")
    (origin / "b.png").write_bytes(b"PNG")
    (origin / "variants").mkdir()
    (origin / ".secret").write_text("hidden")
    (origin / "alias.svg").symlink_to(origin / "a.svg")
    os.mkfifo(origin / "pipe")

    first = files.list_files("data/about/_origin/brand", limit=2)
    assert [item["name"] for item in first["entries"]] == ["a.svg", "b.png"]
    assert first["entries"][0]["mime_type"] == "image/svg+xml"
    assert first["entries"][0]["size_bytes"] == 6
    assert first["total"] == 3
    assert first["next_offset"] == 2
    second = files.list_files(first["directory"], offset=first["next_offset"], limit=2)
    assert second["entries"] == [{
        "file_path": "data/about/_origin/brand/variants", "name": "variants", "type": "directory",
    }]
    assert second["next_offset"] is None
    assert files.list_files(first["directory"], offset=10)["entries"] == []


@pytest.mark.parametrize("path", [
    "../outside", "/etc/passwd", "obsidian/file.png", "data/../outside", "data/.env",
    "data/.graph/db.sqlite", "data/a/../../outside", "data/a\\b", "data/a\0b",
])
def test_rejects_non_corpus_and_hidden_paths(corpus, path):
    with pytest.raises(ValueError):
        files.read_file_bytes(path)
    with pytest.raises(ValueError):
        files.list_files(path)


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 201)])
def test_invalid_pagination(corpus, offset, limit):
    with pytest.raises(ValueError):
        files.list_files(offset=offset, limit=limit)


def test_rejects_symlink_files_and_directories_including_inside_root(corpus, tmp_path):
    (corpus / "real").mkdir()
    (corpus / "real/file.svg").write_bytes(b"<svg/>")
    (tmp_path / "outside.png").write_bytes(b"outside")
    (corpus / "external.png").symlink_to(tmp_path / "outside.png")
    (corpus / "internal.svg").symlink_to(corpus / "real/file.svg")
    (corpus / "alias").symlink_to(corpus / "real", target_is_directory=True)
    for path in ["data/external.png", "data/internal.svg", "data/alias/file.svg"]:
        with pytest.raises(ValueError, match="심볼릭"):
            files.read_file_bytes(path)
    with pytest.raises(ValueError, match="심볼릭"):
        files.list_files("data/alias")


def test_configured_root_symlink_is_supported(corpus, tmp_path, monkeypatch):
    (corpus / "icon.svg").write_bytes(b"<svg/>")
    link = tmp_path / "vault-link"
    link.symlink_to(corpus, target_is_directory=True)
    monkeypatch.setattr("pkb.config.settings.data_root", str(link))
    metadata, data = files.read_file_bytes("data/icon.svg")
    assert metadata["file_path"] == "data/icon.svg"
    assert data == b"<svg/>"


def test_missing_directory_and_special_files_are_rejected(corpus):
    os.mkfifo(corpus / "pipe")
    for path in ["data", "data/missing", "data/pipe"]:
        with pytest.raises(ValueError):
            files.read_file_bytes(path)
    with pytest.raises(ValueError):
        files.list_files("data/missing")


def test_size_limit_is_inclusive_and_empty_file_is_valid(corpus, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 8)
    path = corpus / "bytes.bin"
    for data in [b"", b"12345678"]:
        path.write_bytes(data)
        metadata, actual = files.read_file_bytes("data/bytes.bin")
        assert actual == data
        assert metadata["size_bytes"] == len(data)
        assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    path.write_bytes(b"123456789")
    with pytest.raises(ValueError, match="최대"):
        files.read_file_bytes("data/bytes.bin")


@pytest.mark.parametrize("filename,data,mime", [
    ("로고.svg", b'\xef\xbb\xbf<svg xmlns="http://www.w3.org/2000/svg"/>\r\n', "image/svg+xml"),
    ("icon.png", b"\x89PNG\r\n\x1a\n\x00\xff\x80", "image/png"),
])
@pytest.mark.parametrize("protocol", ["2025-11-25", "2026-07-28"])
def test_http_file_round_trip_and_error(corpus, tmp_path, filename, data, mime, protocol):
    (corpus / filename).write_bytes(data)
    app = mcp_server.mcp.streamable_http_app(host="testserver", **mcp_server._HTTP_TRANSPORT_OPTIONS)
    meta = {"_meta": {
        "io.modelcontextprotocol/protocolVersion": protocol,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "file-client", "version": "1"},
    }} if protocol == "2026-07-28" else {}

    def call(client, name, arguments):
        response = client.post("/mcp", headers={
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": protocol,
            "Mcp-Method": "tools/call", "Mcp-Name": name,
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            **meta, "name": name, "arguments": arguments,
        }})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    with TestClient(app) as client:
        listing = call(client, "list_files", {"directory": "data"})
        assert listing["structuredContent"]["entries"][0]["file_path"] == f"data/{filename}"
        result = call(client, "get_file", {"file_path": f"data/{filename}"})
        assert not result.get("isError")
        metadata = result["structuredContent"]
        resource = next(block["resource"] for block in result["content"] if block["type"] == "resource")
        restored = base64.b64decode(resource["blob"], validate=True)
        output = tmp_path / filename
        output.write_bytes(restored)
        assert output.read_bytes() == data
        assert resource["mimeType"] == metadata["mime_type"] == mime
        assert resource["uri"].startswith("pkb://files/data/")
        assert metadata["size_bytes"] == len(data)
        assert metadata["sha256"] == hashlib.sha256(restored).hexdigest()
        denied = call(client, "get_file", {"file_path": "data/.env"})
        assert denied["isError"] is True
        assert all(block["type"] == "text" for block in denied["content"])
        assert call(client, "list_files", {"directory": "data/.graph"})["isError"] is True
