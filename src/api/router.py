import asyncio
import hashlib
import json
import logging
import queue
import threading
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.schemas import (
    ConfigResponse,
    ConfidenceOut,
    ConflictOut,
    DocumentOut,
    HealthResponse,
    JobOut,
    QueryRequest,
    QueryResponse,
    SentenceOut,
    SourceOut,
)
from src.core.config import SETTINGS
from src.core.errors import DocumentError, GenerationError, ModelUnavailable
from src.core.health import check_health
from src.generation.answerer import Answer, answer_question
from src.ingestion import jobs
from src.ingestion.parsers import validate_upload
from src.ingestion.pipeline import delete, list_indexed
from src.storage import objects

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await jobs.close_pool()


app = FastAPI(title="Smart Document Assistant", version="1.0", lifespan=lifespan)

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


@app.post("/ingest", response_model=JobOut, status_code=202)
async def ingest_document(response: Response, file: UploadFile = File(...)) -> JobOut:
    """Stage the upload and queue it. Parsing and embedding happen in the ingest worker."""
    data = await file.read()
    filename = file.filename or "upload"
    try:
        validate_upload(filename, data)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    doc_id = hashlib.sha256(data).hexdigest()
    job_id = jobs.job_id_for(doc_id)

    # Idempotency key is the content hash: the same bytes in flight are one job, not two.
    holder = await anyio.to_thread.run_sync(jobs.claim, doc_id, job_id)
    if holder is not None:
        state = await anyio.to_thread.run_sync(jobs.get, holder)
        if state is not None:
            response.status_code = 200
            return JobOut(**state)

    await anyio.to_thread.run_sync(jobs.record_queued, job_id, doc_id, filename)
    await anyio.to_thread.run_sync(objects.put_raw, doc_id, filename, data)
    await _enqueue(job_id, doc_id, filename)
    return JobOut(job_id=job_id, doc_id=doc_id, filename=filename, status=jobs.QUEUED, stage="Queued")


async def _enqueue(job_id: str, doc_id: str, filename: str, attempt: int = 0) -> None:
    """Queue the work. Job state lives under job_id; arq's own id changes between attempts,
    because arq keeps the key of a finished job and would silently drop a re-run."""
    arq_id = job_id if attempt == 0 else f"{job_id}:{uuid.uuid4().hex[:8]}"
    enqueued = await (await jobs.pool()).enqueue_job(
        "ingest_job",
        doc_id,
        filename,
        job_id,
        _job_id=arq_id,
        _queue_name=SETTINGS.jobs.queue_name,
    )
    if enqueued is None:
        await _enqueue(job_id, doc_id, filename, attempt + 1)


@app.get("/jobs/dead-letters")
async def dead_letters() -> list[dict]:
    """Poison documents: jobs that failed terminally, newest first."""
    return await anyio.to_thread.run_sync(jobs.dead_letters)


@app.get("/jobs/{job_id}", response_model=JobOut)
async def job_status(job_id: str) -> JobOut:
    state = await anyio.to_thread.run_sync(jobs.get, job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return JobOut(**state)


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    return StreamingResponse(_stream_job(job_id, request), media_type="text/event-stream")


async def _stream_job(job_id: str, request: Request) -> AsyncIterator[str]:
    """Poll the job's Redis state and push every change. Ends on terminal state or disconnect."""
    deadline = asyncio.get_running_loop().time() + SETTINGS.jobs.progress_timeout_s
    last: tuple[str, str] = ("", "")
    while True:
        if await request.is_disconnected():
            return
        state = await anyio.to_thread.run_sync(jobs.get, job_id)
        if state is None:
            yield _sse("error", {"detail": "Unknown job."})
            return
        current = (state["status"], state.get("stage", ""))
        if current != last:
            last = current
            if state["status"] == jobs.DONE:
                yield _sse("done", JobOut(**state).model_dump())
                return
            if state["status"] == jobs.FAILED:
                yield _sse("error", {"detail": state.get("error", "Ingest failed.")})
                return
            yield _sse("progress", {"status": state["status"], "stage": current[1]})
        if asyncio.get_running_loop().time() > deadline:
            yield _sse("error", {"detail": "Timed out waiting for ingest to finish."})
            return
        await asyncio.sleep(SETTINGS.jobs.progress_poll_s)


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
