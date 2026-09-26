import json
import logging
import queue
import threading

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.schemas import (
    ConfigResponse,
    ConfidenceOut,
    ConflictOut,
    DocumentOut,
    HealthResponse,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    SentenceOut,
    SourceOut,
)
from src.core.config import SETTINGS
from src.core.errors import DocumentError, GenerationError, ModelUnavailable
from src.core.health import check_health
from src.generation.answerer import Answer, answer_question
from src.ingestion.parsers import validate_upload
from src.ingestion.pipeline import delete, ingest, list_indexed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Smart Document Assistant", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/livez")
def livez() -> dict[str, str]:
    """Liveness: the process is running. Never touches a backend."""
    return {"status": "ok"}


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(**check_health())


@app.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return ConfigResponse(
        retrieval_modes=list(SETTINGS.retrieval.modes),
        default_mode=SETTINGS.retrieval.mode,
        max_question_chars=SETTINGS.max_question_chars,
    )


@app.get("/documents", response_model=list[DocumentOut])
def documents() -> list[DocumentOut]:
    return [DocumentOut(**doc) for doc in list_indexed()]


@app.post("/ingest", response_model=IngestResponse)
async def ingest_document(file: UploadFile = File(...)) -> IngestResponse:
    data = await file.read()
    filename = file.filename or "upload"
    try:
        validate_upload(filename, data)
        report = ingest(
            filename, data, deps.embedder(), captioner=deps.figure_captioner()
        )
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return IngestResponse(
        doc_id=report.doc_id,
        filename=report.filename,
        pages=report.pages,
        num_parents=report.num_parents,
        num_children=report.num_children,
        outcome=report.outcome,
    )


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str) -> dict[str, str]:
    delete(doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@app.post("/query")
def query(request: QueryRequest) -> StreamingResponse:
    _validate_query(request)
    return StreamingResponse(_stream_answer(request), media_type="text/event-stream")


def _validate_query(request: QueryRequest) -> None:
    if request.mode not in SETTINGS.retrieval.modes:
        raise HTTPException(status_code=400, detail=f"Unknown mode '{request.mode}'.")
    stripped = request.question.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="Question cannot be empty.")
    if len(stripped) > SETTINGS.max_question_chars:
        raise HTTPException(status_code=422, detail="Question exceeds character limit.")
    non_alnum = sum(1 for c in stripped if not c.isalnum() and not c.isspace())
    if len(stripped) > 0 and non_alnum / len(stripped) > 0.6:
        raise HTTPException(status_code=422, detail="Question contains too many special characters.")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_answer(request: QueryRequest):
    events: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def on_token(text: str) -> None:
        events.put(("token", {"text": text}))

    def run() -> None:
        try:
            answer = answer_question(
                request.question,
                request.doc_ids,
                deps.embedder(),
                deps.vector_store(),
                deps.llm_client(),
                keyword_index=deps.keyword_index(),
                reranker=deps.reranker(),
                mode=request.mode,
                generate=request.generate,
                on_token=on_token,
                history=request.history,
            )
            events.put(("done", _to_response(answer).model_dump()))
        except (ModelUnavailable, GenerationError) as exc:
            events.put(("error", {"detail": str(exc)}))
        except Exception:
            logger.exception("Unhandled error while streaming answer")
            events.put(("error", {"detail": "Internal server error."}))
        finally:
            events.put(("__end__", {}))

    threading.Thread(target=run, daemon=True).start()

    while True:
        event, data = events.get()
        if event == "__end__":
            return
        yield _sse(event, data)


def _to_response(answer: Answer) -> QueryResponse:
    return QueryResponse(
        query_id=answer.trace.query_id,
        status=answer.status,
        answer_text=answer.answer_text,
        sentences=[
            SentenceOut(text=s.text, cites=s.cites, citation_status=s.citation_status)
            for s in answer.sentences
        ],
        sources=[
            SourceOut(
                id=s.id,
                filename=s.filename,
                pages=s.pages,
                section_path=s.section_path,
                text=s.text,
                char_span_in_parent=list(s.char_span_in_parent),
                score=s.score,
            )
            for s in answer.sources
        ],
        conflicts=[ConflictOut(claim=c.claim, source_ids=c.source_ids) for c in answer.conflicts],
        confidence=(
            ConfidenceOut(
                label=answer.confidence.label,
                score=answer.confidence.score,
                components=answer.confidence.components,
            )
            if answer.confidence
            else None
        ),
        abstain_reason=answer.abstain_reason,
        suggestions=[
            {"text": s.text, "filename": s.filename, "pages": s.pages, "label": s.label}
            for s in answer.suggestions
        ],
        near_miss=(
            {
                "filename": answer.near_miss.filename,
                "page_start": answer.near_miss.page_start,
                "page_end": answer.near_miss.page_end,
                "text": answer.near_miss.text,
                "score": answer.near_miss.score,
                "section_path": list(answer.near_miss.section_path),
            }
            if answer.near_miss
            else None
        ),
        trace={
            "query_id": answer.trace.query_id,
            "stages": [
                {"name": s.name, "duration_s": s.duration_s, "payload": s.payload}
                for s in answer.trace.stages
            ],
        },
    )
