from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    es_host: str = "http://localhost:9200"
    es_index: str = "pkb_documents"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dims: int = 384
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_enabled: bool = True
    fusion: str = "rrf"  # "rrf" 또는 "native"
    candidate_k: int = 50
    expand_context: int = 0  # N>0이면 각 검색 결과의 ±N 청크를 neighbors로 부착
    chunk_size: int = 500
    chunk_overlap: int = 100
    default_top_k: int = 5
    obsidian_path: str = ""  # Obsidian 볼트 절대경로 (비어있으면 비활성화)
    graph_db_path: str = "data/.graph/pkb_graph.sqlite"  # 개념 그래프 SQLite 파일
    graph_extract_model: str = "claude-haiku-4-5-20251001"  # 개념/관계 추출 LLM
    graph_dedup_threshold: float = 0.88  # 임베딩 유사도 기반 개념 병합 임계값

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
