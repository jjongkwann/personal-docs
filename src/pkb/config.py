from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    es_host: str = "http://localhost:9200"
    es_index: str = "pkb_documents"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dims: int = 384
    embedding_device: str = "auto"  # auto | cpu | mps | cuda
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_enabled: bool = True
    rerank_device: str = "auto"  # auto | cpu | mps | cuda
    rerank_batch_size: int = 8  # MPS에선 작은 배치가 더 빠름 (bench_rerank_models.py 결과)
    warmup_on_start: bool = True  # MCP 서버 기동 시 백그라운드로 모델·ES 워밍업
    candidate_k: int = 20  # 기본 경로(rerank=on)에서 ck=50 대비 latency 2.4x↓, 품질 동일. RRF-only도 nDCG 미세 우위.
    expand_context: int = 0  # N>0이면 각 검색 결과의 ±N 청크를 neighbors로 부착
    chunk_size: int = 500
    chunk_overlap: int = 100
    default_top_k: int = 5
    obsidian_path: str = ""  # Obsidian 볼트 절대경로 (비어있으면 비활성화)
    data_root: str = "data"  # 개인 코퍼스 루트. 볼트 하위 절대경로로 두면 Obsidian과 원본 공유(SSOT)
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
