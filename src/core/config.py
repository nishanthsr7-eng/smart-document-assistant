import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT_DIR
    data: Path = ROOT_DIR / "data"
    chroma: Path = ROOT_DIR / "data" / "chroma"
    models: Path = ROOT_DIR / "data" / "models"
    parents: Path = ROOT_DIR / "data" / "parents"
    sample_docs: Path = ROOT_DIR / "data" / "sample_docs"


@dataclass(frozen=True)
class ModelConfig:
    embedder_name: str = "BAAI/bge-base-en-v1.5"
    embedder_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedder_max_tokens: int = 512
    reranker_name: str = "mixedbread-ai/mxbai-rerank-base-v1"
    blip_name: str = "Salesforce/blip-image-captioning-base"
    # Generation provider: "gemini" (default), "groq", or "ollama" (offline fallback).
    llm_provider: str = os.environ.get("LLM_PROVIDER", "gemini").lower()
    ollama_host: str = os.environ.get("OLLAMA_HOST", "127.0.0.1:11435")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
    gemini_model: str = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
    groq_model: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


@dataclass(frozen=True)
class IngestionConfig:
    child_tokens: int = 200
    child_overlap_sentences: int = 1
    parent_tokens: int = 800
    max_upload_mb: int = 20
    max_pages: int = 200
    min_chars_per_page: int = 40
    parent_cache_size: int = 32


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "hybrid_rerank"
    modes: tuple[str, ...] = ("dense", "hybrid", "hybrid_rerank")
    dense_k: int = 30
    lexical_k: int = 30
    rrf_k: int = 60
    rerank_candidates: int = 10
    context_k: int = 4
    context_token_budget: int = 3000
    max_per_doc: int = 2
    query_expansion: bool = True
    expansion_variants: int = 3
    hyde: bool = False
    mmr_lambda: float = 0.7
    span_padding_tokens: int = 200
    dedup_jaccard: float = 0.9
    unused_passages_k: int = 6
    max_suggestions: int = 3
    calibration_logistic_a: float = 1.0
    calibration_logistic_b: float = 0.0


@dataclass(frozen=True)
class TrustConfig:
    abstain_threshold: float = 0.3
    abstain_soft_margin: float = 0.15
    min_source_score: float = 0.2
    support_threshold: float = 0.5
    grounding_threshold: float = 0.8
    confidence_high_edge: float = 0.75
    confidence_medium_edge: float = 0.45


@dataclass(frozen=True)
class GenerationConfig:
    temperature: float = 0.1
    max_tokens: int = 1024
    num_ctx: int = 8192
    timeout_s: int = 60
    keep_alive: str = "30m"
    history_turns: int = 6
    cache_ttl_s: int = 3600
    cache_size: int = 128


@dataclass(frozen=True)
class Settings:
    paths: Paths
    models: ModelConfig
    ingestion: IngestionConfig
    retrieval: RetrievalConfig
    trust: TrustConfig
    generation: GenerationConfig
    max_question_chars: int
    ingest_version: str


def _build_ingest_version(ingestion: IngestionConfig, embedder_name: str) -> str:
    payload = f"{ingestion.child_tokens}:{ingestion.child_overlap_sentences}:{ingestion.parent_tokens}:{embedder_name}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _build_settings() -> Settings:
    ingestion = IngestionConfig()
    models = ModelConfig()
    return Settings(
        paths=Paths(),
        models=models,
        ingestion=ingestion,
        retrieval=RetrievalConfig(),
        trust=TrustConfig(),
        generation=GenerationConfig(),
        max_question_chars=2000,
        ingest_version=_build_ingest_version(ingestion, models.embedder_name),
    )


SETTINGS = _build_settings()
