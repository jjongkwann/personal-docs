from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    es_host: str = "http://localhost:9200"
    es_index: str = "pkb_documents"
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dims: int = 384
    chunk_size: int = 500
    chunk_overlap: int = 100
    default_top_k: int = 5
    obsidian_path: str = ""  # Obsidian 볼트 절대경로 (비어있으면 비활성화)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
