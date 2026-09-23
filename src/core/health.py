from src.core.config import SETTINGS
from src.generation.client import build_client
from src.retrieval.embedder import Embedder
from src.retrieval.vector_store import VectorStore


def check_health() -> dict[str, str]:
    checks: dict[str, str] = {}

    try:
        Embedder().encode(["health check"])
        checks["embedder"] = "ok"
    except Exception as exc:
        checks["embedder"] = f"error: {exc}"

    try:
        VectorStore().all_chunks()
        checks["vector_store"] = "ok"
    except Exception as exc:
        checks["vector_store"] = f"error: {exc}"

    try:
        build_client().health()
        checks["llm"] = "ok"
    except Exception as exc:
        checks["llm"] = f"error: {exc}"

    checks["status"] = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    checks["provider"] = SETTINGS.models.llm_provider
    return checks
