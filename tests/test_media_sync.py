"""`pkb media-sync` 클라이언트: 30초 창·무음 처리, MCP 이름 찾기, 내려받기·임베딩·올리기 흐름."""

from __future__ import annotations

import base64
import hashlib
import io
import json

import numpy as np
import pytest

from pkb import media_sync
from pkb.media_sync import MODEL, SAMPLE_RATE, McpClient, ToolError, audio_windows, run


def _tone(seconds: float, amplitude: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def test_audio_windows_cut_30_seconds_and_skip_silence_and_short_tail():
    samples = np.concatenate([_tone(30), _silence(30), _tone(10), _silence(30), _silence(0.3)])
    windows = audio_windows(samples)
    assert [(start, end) for start, end, _ in windows] == [(0.0, 30.0), (60.0, 90.0)]
    assert all(len(piece) == 30 * SAMPLE_RATE for _, _, piece in windows)


def test_all_silent_audio_keeps_its_loudest_window():
    samples = np.concatenate([_silence(30), _tone(5, amplitude=0.001), _silence(25)])
    (window,) = audio_windows(samples)
    assert window[:2] == (30.0, 60.0)


class _Response:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_client_uses_gateway_names_headers_and_token(monkeypatch):
    sent: list[tuple[dict, dict]] = []

    def post(url, json, headers, timeout):
        sent.append((json, headers))
        if json["method"] == "tools/list":
            tools = [{"name": "pkb-list-media"}, {"name": "pkb-get-media"}, {"name": "other"}]
            return _Response({"jsonrpc": "2.0", "id": json["id"], "result": {"tools": tools}})
        text = "오류: ValueError: 안 됨" if json["params"]["arguments"].get("bad") else '{"ok": true}'
        return _Response({"result": {"content": [{"type": "text", "text": text}]}})

    monkeypatch.setattr(media_sync.httpx, "post", post)
    client = McpClient("https://gateway.example/mcp", token="secret")
    client.connect(("list_media", "get_media"))
    assert client.call_json("list_media", limit=1) == {"ok": True}

    body, headers = sent[-1]
    assert body["method"] == "tools/call" and body["params"] == {"name": "pkb-list-media", "arguments": {"limit": 1}}
    assert headers["Mcp-Method"] == "tools/call" and headers["Mcp-Name"] == "pkb-list-media"
    assert headers["Authorization"] == "Bearer secret"
    with pytest.raises(ToolError, match="안 됨"):
        client.call("get_media", bad=True)
    with pytest.raises(ToolError, match="put_media_embeddings"):
        client.connect(("put_media_embeddings",))


def test_client_reports_rejected_token(monkeypatch):
    monkeypatch.setattr(media_sync.httpx, "post", lambda *args, **kwargs: _Response({}, status=401))
    with pytest.raises(ToolError, match="401"):
        McpClient("https://gateway.example/mcp").connect(("list_media",))


class _FakeServer:
    """list_media·get_media·put_media_embeddings만 흉내 낸다."""

    def __init__(self, files: dict[str, tuple[str, bytes]], model: str | None = MODEL):
        self.files = files
        self.model = model
        self.puts: list[dict] = []
        self.downloads: list[str] = []

    def connect(self, tools):
        pass

    def call_json(self, tool: str, **args) -> dict:
        if tool == "list_media":
            media = [
                {"doc_id": doc_id, "kind": kind, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                 "indexed": False, "model": None, "modified": "2026-10-15T00:00:00+00:00"}
                for doc_id, (kind, data) in sorted(self.files.items())
            ]
            return {"total": len(media), "model": self.model, "media": media[args["offset"]:]}
        if tool == "get_media":
            self.downloads.append(args["doc_id"])
            data = self.files[args["doc_id"]][1]
            return {"data_base64": base64.b64encode(data).decode(), "length": len(data), "eof": True,
                    "sha256": hashlib.sha256(data).hexdigest()}
        assert tool == "put_media_embeddings"
        self.puts.append(args)
        return {"linked_from": []}


class _FakeEmbedder:
    load_seconds = 0.0

    def image(self, image):
        return [1.0, 0.0]

    def audio(self, windows):
        return [[0.0, 1.0] for _ in windows]


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_run_embeds_images_then_audio_and_uploads_segments(monkeypatch):
    monkeypatch.setattr(media_sync, "decode_audio", lambda path: np.concatenate([_tone(30), _tone(12)]))
    server = _FakeServer({
        "attachments/m/others.m4a": ("audio", b"audio-bytes"),
        "obsidian/a.png": ("image", _png()),
        "obsidian/broken.jpg": ("image", b"not an image"),
    })

    stats = run(server, _FakeEmbedder(), ["image", "audio"], echo=lambda line: None)

    assert (stats["indexed"], stats["skipped"], stats["failed"]) == (2, 1, 0)
    assert server.downloads == ["obsidian/a.png", "obsidian/broken.jpg", "attachments/m/others.m4a"]
    image, audio = server.puts
    assert image == {"doc_id": "obsidian/a.png", "kind": "image", "sha256": hashlib.sha256(_png()).hexdigest(),
                     "model": MODEL, "segments": [{"vector": [1.0, 0.0]}]}
    assert [(s["start"], s["end"]) for s in audio["segments"]] == [(0.0, 30.0), (30.0, 42.0)]
    assert stats["segments"] == 2


def test_run_dry_run_and_model_checks(monkeypatch):
    server = _FakeServer({"obsidian/a.png": ("image", _png())})
    lines: list[str] = []
    run(server, None, ["image"], dry_run=True, echo=lines.append)
    assert server.downloads == [] and "obsidian/a.png" in lines[-1]

    with pytest.raises(ToolError, match="꺼져"):
        run(_FakeServer({}, model=None), None, ["image"], echo=lines.append)
    with pytest.raises(ToolError, match="other/model"):
        run(_FakeServer({}, model="other/model"), None, ["image"], echo=lines.append)


def test_download_rejects_changed_file(tmp_path):
    server = _FakeServer({"obsidian/a.png": ("image", b"new")})
    with pytest.raises(ToolError, match="sha256"):
        media_sync.download(server, "obsidian/a.png", hashlib.sha256(b"old").hexdigest(), tmp_path / "x")


def test_load_image_flattens_transparency_on_white(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(_png())
    image = media_sync.load_image(path)
    assert image.mode == "RGB" and image.getpixel((0, 0)) == (255, 255, 255)
