"""`pkb media-sync`: 이 Mac에서 이미지·오디오 벡터를 만들어 PKB 서버에 올린다.

PKB 서버(메모리 8GB mini)는 미디어 모델을 올리지 않는다. 흐름은 list_media(missing_only) →
get_media → EmbeddingGemma 2 → put_media_embeddings.
서버 URL은 PKB 서버(`http://<host>:8787/mcp`)나 MCP 게이트웨이 어느 쪽이든 된다. 게이트웨이는 도구를
`pkb-` + 하이픈 이름으로 내놓으므로 tools/list에서 이름을 찾는다.

의존성: `uv sync --extra media` (모델 프로세서가 torchvision을 쓴다). 오디오는 macOS afconvert로
16 kHz 모노로 풀고, PIL이 못 여는 SVG·HEIC는 macOS sips로 PNG를 만든다.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import shutil
import subprocess
import tempfile
import time
import wave
from collections.abc import Callable
from pathlib import Path

import httpx

MODEL = "google/embeddinggemma-2"
SAMPLE_RATE = 16000
WINDOW_SECONDS = 30  # 창 하나가 검색 결과 하나다 (모델은 5분 남짓까지 받지만 짧아야 찾은 곳부터 튼다)
MIN_TAIL_SECONDS = 0.5  # 이보다 짧은 마지막 창은 버린다
SILENCE_DBFS = -45.0  # 창의 50ms 프레임 RMS 95백분위가 이보다 작으면 무음으로 본다
CHUNK_BYTES = 4 * 1024 * 1024


class ToolError(RuntimeError):
    """서버·게이트웨이가 요청을 거절했다."""


class McpClient:
    """무상태 Streamable HTTP MCP의 tools/list·tools/call만 쓰는 클라이언트 (회의 앱 PKBClient와 같은 요청)."""

    def __init__(self, url: str, token: str | None = None, timeout: float = 600) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._names: dict[str, str] = {}

    def _post(self, method: str, params: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
        }
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            response = httpx.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ToolError(f"{self.url}에 연결하지 못했습니다: {exc}") from exc
        if response.status_code == 401:
            raise ToolError("토큰을 받아들이지 않았습니다 (HTTP 401). 토큰 파일을 확인하세요.")
        if response.status_code != 200:
            raise ToolError(f"HTTP {response.status_code}: {response.text[:300]}")
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            lines = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            payload = json.loads(lines[-1])
        else:
            payload = response.json()
        if "error" in payload:
            raise ToolError(str(payload["error"].get("message") or payload["error"]))
        return payload["result"]

    def connect(self, tools: tuple[str, ...]) -> None:
        """서버가 쓰는 이름을 찾는다. PKB 서버는 그대로, 게이트웨이는 pkb-list-media 꼴."""
        available: set[str] = set()
        cursor = None
        while True:
            result = self._post("tools/list", {"cursor": cursor} if cursor else {})
            available.update(tool["name"] for tool in result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        for tool in tools:
            gateway = "pkb-" + tool.replace("_", "-")
            if tool in available:
                self._names[tool] = tool
            elif gateway in available:
                self._names[tool] = gateway
            else:
                raise ToolError(
                    f"서버에 {tool} 도구가 없습니다 (PKB 서버를 업데이트하고 게이트웨이 도구 목록을 새로 고치세요)."
                )

    def call(self, tool: str, **arguments) -> str:
        result = self._post("tools/call", {"name": self._names.get(tool, tool), "arguments": arguments})
        text = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        # PKB 도구는 실패도 "오류: …"로 시작하는 정상 응답으로 돌려준다
        if result.get("isError") or text.startswith("오류"):
            raise ToolError(text)
        return text

    def call_json(self, tool: str, **arguments) -> dict:
        return json.loads(self.call(tool, **arguments))


class MediaEmbedder:
    """EmbeddingGemma 2로 이미지·오디오 벡터를 만든다 (MPS면 bf16). 미디어에는 프롬프트를 붙이지 않는다."""

    def __init__(self, device: str = "auto", model_id: str = MODEL) -> None:
        import importlib.util

        from pkb.config import resolve_device

        # 이미지 프로세서가 torchvision을 쓴다
        if importlib.util.find_spec("torchvision") is None:
            raise ToolError(
                "torchvision이 없습니다. `uv sync --extra media` 뒤 `uv run --extra media pkb media-sync`로 실행하세요."
            )
        self.device = resolve_device(device)
        self.model_id = model_id
        self._model = None
        self.load_seconds = 0.0  # 모델 적재 시간 — 처리량에서 따로 뺀다

    def _load(self):
        if self._model is not None:
            return self._model
        import torch
        from sentence_transformers import SentenceTransformer

        started = time.perf_counter()
        dtype = torch.float32 if self.device == "cpu" else torch.bfloat16  # float16은 이 모델에서 NaN이 난다
        kwargs = {"device": self.device, "model_kwargs": {"dtype": dtype}}
        try:  # 캐시에 있으면 허브에 묻지 않는다
            self._model = SentenceTransformer(self.model_id, local_files_only=True, **kwargs)
        except OSError:
            self._model = SentenceTransformer(self.model_id, **kwargs)
        self.load_seconds += time.perf_counter() - started
        return self._model

    def _encode(self, inputs: list) -> list[list[float]]:
        vectors = self._load().encode(inputs, normalize_embeddings=True, show_progress_bar=False)
        if self.device == "mps":
            import torch

            torch.mps.empty_cache()
        # bf16 벡터라 소수 6자리면 충분하다 — 요청 크기가 절반으로 준다
        return [[round(float(value), 6) for value in vector] for vector in vectors]

    def image(self, image) -> list[float]:
        return self._encode([image])[0]

    def audio(self, windows: list) -> list[list[float]]:
        return self._encode([{"array": window, "sampling_rate": SAMPLE_RATE} for window in windows])


def load_image(path: Path):
    """PIL 이미지(RGB, 투명은 흰 바탕). PIL이 못 여는 SVG·HEIC는 sips로 PNG를 만든다. 못 열면 None."""
    from PIL import Image, UnidentifiedImageError

    def flatten(image):
        if image.mode in {"RGBA", "LA", "P"}:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            return Image.alpha_composite(background, rgba).convert("RGB")
        return image.convert("RGB")

    try:
        with Image.open(path) as image:
            return flatten(image)
    except (UnidentifiedImageError, OSError):
        pass
    if not shutil.which("sips"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        rendered = Path(tmp) / "rendered.png"
        done = subprocess.run(
            ["sips", "-s", "format", "png", str(path), "--out", str(rendered)], capture_output=True,
        )
        if done.returncode != 0 or not rendered.exists():
            return None
        with Image.open(rendered) as image:
            return flatten(image)


def decode_audio(path: Path):
    """afconvert로 16 kHz 모노 16비트 PCM을 풀어 [-1, 1] float32 배열로."""
    import numpy as np

    if not shutil.which("afconvert"):
        raise ToolError("afconvert가 없습니다 (macOS에서 실행하세요).")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "audio.wav"
        done = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(path), str(target)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise ToolError(f"오디오를 풀지 못했습니다: {done.stderr.strip() or done.returncode}")
        with wave.open(str(target)) as stream:
            frames = stream.readframes(stream.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def _level_dbfs(samples) -> float:
    """50ms 프레임 RMS의 95백분위 (dBFS). 말소리 한두 마디도 잡고, 잡음 바닥은 넘지 않게."""
    import numpy as np

    frame = SAMPLE_RATE // 20
    usable = len(samples) // frame * frame
    frames = samples[:usable].reshape(-1, frame) if usable else samples.reshape(1, -1)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    level = float(np.percentile(rms, 95)) if rms.size else 0.0
    return 20 * np.log10(max(level, 1e-10))


def audio_windows(samples) -> list[tuple[float, float, object]]:
    """(start초, end초, 샘플) 30초 창. 무음 창은 버리되, 전부 무음이면 가장 큰 창 하나는 남긴다."""
    size = WINDOW_SECONDS * SAMPLE_RATE
    windows = []
    for start in range(0, len(samples), size):
        piece = samples[start : start + size]
        if windows and len(piece) < MIN_TAIL_SECONDS * SAMPLE_RATE:
            continue
        windows.append((start / SAMPLE_RATE, (start + len(piece)) / SAMPLE_RATE, piece))
    levels = [_level_dbfs(piece) for _, _, piece in windows]
    voiced = [window for window, level in zip(windows, levels, strict=True) if level >= SILENCE_DBFS]
    if not voiced and windows:
        voiced = [windows[max(range(len(windows)), key=levels.__getitem__)]]
    return voiced


def download(client: McpClient, doc_id: str, sha256: str, target: Path) -> None:
    offset = 0
    with target.open("wb") as stream:
        while True:
            part = client.call_json("get_media", doc_id=doc_id, offset=offset, length=CHUNK_BYTES)
            stream.write(base64.b64decode(part["data_base64"]))
            offset += part["length"]
            if part["eof"]:
                break
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != sha256 or part.get("sha256") != sha256:
        raise ToolError(f"받은 파일의 sha256이 목록과 다릅니다 (파일이 바뀌었으면 다시 실행): {doc_id}")


def pending_media(client: McpClient, kinds: list[str]) -> tuple[str | None, list[dict]]:
    """(서버가 받는 모델, 미색인 파일). 이미지 먼저, 그다음 오디오."""
    entries: list[dict] = []
    model = None
    while True:
        page = client.call_json("list_media", kinds=kinds, missing_only=True, limit=1000, offset=len(entries))
        model = page["model"]
        entries.extend(page["media"])
        if not page["media"] or len(entries) >= page["total"]:
            break
    order = {"image": 0, "audio": 1}
    return model, sorted(entries, key=lambda entry: (order[entry["kind"]], entry["doc_id"]))


def run(
    client: McpClient,
    embedder: MediaEmbedder | None,
    kinds: list[str],
    limit: int = 0,
    dry_run: bool = False,
    echo: Callable[[str], None] = print,
) -> dict:
    """미색인 미디어를 임베딩해 올린다. 반환: 처리·건너뜀·실패 수와 임베딩 시간."""
    client.connect(("list_media", "get_media", "put_media_embeddings"))
    server_model, pending = pending_media(client, kinds)
    if server_model is None:
        raise ToolError(
            "서버의 미디어 색인이 꺼져 있거나 EMBEDDING_MODEL과 공간이 맞지 않습니다 (MEDIA_EMBEDDING_MODEL)."
        )
    if server_model != MODEL:
        raise ToolError(f"서버는 {server_model} 벡터를 받습니다. 이 클라이언트는 {MODEL}만 만듭니다.")
    total = len(pending)
    if limit:
        pending = pending[:limit]
    echo(f"색인할 파일 {len(pending)}개 (미색인 {total}개, 모델 {MODEL})")
    stats = {"indexed": 0, "skipped": 0, "failed": 0, "images": 0, "image_seconds": 0.0,
             "segments": 0, "audio_seconds": 0.0, "audio_duration": 0.0}
    if dry_run:
        for entry in pending:
            echo(f"  {entry['kind']:5} {entry['size_bytes']:>10,} B  {entry['doc_id']}")
        return stats
    assert embedder is not None
    started = time.perf_counter()
    for number, entry in enumerate(pending, 1):
        doc_id, kind = entry["doc_id"], entry["kind"]
        prefix = f"[{number}/{len(pending)}] {kind} {doc_id}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / ("media" + Path(doc_id).suffix.lower())
                download(client, doc_id, entry["sha256"], local)
                if kind == "image":
                    image = load_image(local)
                    if image is None:
                        echo(f"{prefix}: 건너뜀 — 이 Mac에서 열 수 없는 이미지 (SVG·HEIC는 sips가 필요)")
                        stats["skipped"] += 1
                        continue
                    clock, loads = time.perf_counter(), embedder.load_seconds
                    segments = [{"vector": embedder.image(image)}]
                    elapsed = time.perf_counter() - clock - (embedder.load_seconds - loads)
                    stats["images"] += 1
                    stats["image_seconds"] += elapsed
                    detail = f"{image.size[0]}x{image.size[1]}, {elapsed:.1f}s"
                else:
                    samples = decode_audio(local)
                    windows = audio_windows(samples)
                    if not windows:
                        echo(f"{prefix}: 건너뜀 — 소리가 없는 파일")
                        stats["skipped"] += 1
                        continue
                    clock, loads = time.perf_counter(), embedder.load_seconds
                    vectors = embedder.audio([piece for _, _, piece in windows])
                    elapsed = time.perf_counter() - clock - (embedder.load_seconds - loads)
                    segments = [
                        {"start": start, "end": end, "vector": vector}
                        for (start, end, _), vector in zip(windows, vectors, strict=True)
                    ]
                    duration = len(samples) / SAMPLE_RATE
                    stats["segments"] += len(segments)
                    stats["audio_seconds"] += elapsed
                    stats["audio_duration"] += duration
                    detail = f"{duration:.0f}초 중 {len(segments)}개 구간, {elapsed:.1f}s"
            reply = client.call_json(
                "put_media_embeddings", doc_id=doc_id, kind=kind, sha256=entry["sha256"], model=MODEL,
                segments=segments,
            )
            linked = f", 노트 {len(reply['linked_from'])}개" if reply.get("linked_from") else ""
            echo(f"{prefix}: {detail}{linked}")
            stats["indexed"] += 1
        except ToolError as exc:
            echo(f"{prefix}: 실패 — {exc}")
            stats["failed"] += 1
    stats["wall_seconds"] = time.perf_counter() - started
    stats["load_seconds"] = embedder.load_seconds
    return stats
