from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    es_host: str = "http://localhost:9200"
    es_index: str = "pkb_documents"
    embedding_model: str = "BAAI/bge-m3"  # 2026-07 매트릭스 실측 채택 (KURE-v1·MiniLM 대비 우위)
    embedding_revision: str = ""  # 선택: 모델 저장소 commit/tag 고정
    embedding_dims: int = 1024
    embedding_device: str = "auto"  # auto | cpu | mps | cuda
    # 2026-10 mini(8GB) 실측: float16은 gold 57문항에서 Recall@5·근거 Recall 동일, MRR@5 0.526→0.523, 적재 17→6초
    # EmbeddingGemma 2는 float16에서 NaN이나 망가진 벡터를 내므로 bfloat16을 쓴다.
    # GPU(mps·cuda)에서만 적용, CPU는 항상 float32
    embedding_dtype: Literal["float32", "float16", "bfloat16"] = "float16"
    embedding_batch_size: int = 32  # sentence-transformers 기본값. RAM 8GB 머신의 대량 색인에선 8까지 낮출 것
    # 질의와 문서에 다른 입력을 쓰는 모델용. 비워 두면(bge-m3) encode에 아무것도 더하지 않는다.
    # google/embeddinggemma-2: dims=768, dtype=bfloat16, text_only=true,
    # query_prompt=SearchQuery, document_prompt=Document, normalize=true
    # jinaai/jina-embeddings-v5-text-small: trust_remote_code=true, task=retrieval,
    # query_prompt=query, document_prompt=document, normalize=true (peft 필요)
    embedding_trust_remote_code: bool = False  # 모델 저장소의 코드를 실행한다 — 믿는 모델만
    embedding_task: str = ""  # encode(task=...) 어댑터 이름
    embedding_query_prompt: str = ""  # 질의 encode의 prompt_name
    embedding_document_prompt: str = ""  # 문서 encode의 prompt_name
    embedding_normalize: bool = False  # encode(normalize_embeddings=True)
    # 이미지·오디오 인코더를 빼고 텍스트 탑만 올린다 (EmbeddingGemma 2: 740M → 270M). 질의·청크는 글뿐이다
    embedding_text_only: bool = False
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_revision: str = ""  # 선택: 모델 저장소 commit/tag 고정
    # 2026-07 벤치: BGE-M3 후보 풀에서 bge/qwen3 리랭커 모두 no-rerank(MRR 0.517)보다 낮아 기본 off
    rerank_enabled: bool = False
    rerank_device: str = "auto"  # auto | cpu | mps | cuda
    rerank_batch_size: int = 8  # MPS에선 작은 배치가 더 빠름 (bench_rerank_models.py 결과)
    mcp_host: str = "127.0.0.1"  # 바인딩 주소. 0.0.0.0으로 열 때는 ACCESS_POLICY로 인증을 켤 것
    mcp_port: int = 8787  # 공유 HTTP MCP 서버 포트
    # 접근 제어 정책 YAML (pkb.access). 비우면 접근 제어가 꺼진 단일 사용자 모드 — 모든 호출이 관리자다.
    # 설정하면 MCP 요청마다 Bearer 토큰이 필요하고 경로 규칙에 없는 자원은 거부된다.
    access_policy: str = ""
    # 클라이언트가 보는 MCP 주소(OAuth 보호 자원 메타데이터의 resource). 비우면 http://MCP_HOST:MCP_PORT/mcp
    mcp_public_url: str = ""
    warmup_on_start: bool = False  # 기동 시 모델 예열. 끄면 첫 검색까지 서버가 ~50MB로 유휴
    model_idle_seconds: float = 300  # 마지막 사용 뒤 이 시간이 지나면 모델을 내려 메모리를 돌려준다. 0이면 계속 유지
    candidate_k: int = 20  # 기본 경로(rerank=on)에서 ck=50 대비 latency 2.4x↓, 품질 동일. RRF-only도 nDCG 미세 우위.
    expand_context: int = 0  # N>0이면 각 검색 결과의 ±N 청크를 neighbors로 부착
    chunk_size: int = 500
    chunk_overlap: int = 100
    embed_context_prefix: bool = True  # 임베딩 입력에 title·section_path prefix (rrf MRR 0.499→0.517 실측)
    default_top_k: int = 5
    obsidian_path: str = ""  # Obsidian 볼트 절대경로 (비어있으면 비활성화)
    data_root: str = "data"  # 개인 코퍼스 루트. 볼트 하위 절대경로로 두면 Obsidian과 원본 공유(SSOT)
    # 볼트 밖 첨부(회의 오디오 등) 루트. put_attachment/get_attachment가 이 아래만 다룬다. 비우면 꺼짐
    attachments_root: str = ""
    # 이미지·오디오 벡터를 만드는 모델 (클라이언트 `pkb media-sync`가 돌린다). 비우면 미디어 색인·검색 꺼짐.
    # EMBEDDING_MODEL과 벡터 공간이 같은 모델만 받는다 (pkb.media.MEDIA_SPACES)
    media_embedding_model: str = ""
    graph_db_path: str = "data/.graph/pkb_graph.sqlite"  # 개념 그래프 SQLite 파일
    graph_dedup_threshold: float = 0.88  # 임베딩 유사도 기반 개념 병합 임계값

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


def data_dir() -> Path:
    """개인 코퍼스 루트 절대경로. 위치와 무관하게 doc_id는 'data/<상대경로>'로 고정된다."""
    return Path(settings.data_root).expanduser().resolve()


def resolve_device(name: str) -> str:
    """auto면 mps > cuda > cpu 순으로 선택. 그 외는 그대로 반환."""
    if name != "auto":
        return name
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"
