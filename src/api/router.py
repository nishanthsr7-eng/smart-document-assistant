import asyncio
import contextvars
import json
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator, Awaitable, Callable

import anyio
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind

from src.api import deps
from src.api.schemas import (
    AuditEventOut,
    ConfidenceOut,
    ConfigResponse,
    ConflictOut,
    CreateUserRequest,
    DocumentOut,
    HealthResponse,
    JobOut,
    LoginRequest,
    QueryRequest,
    QueryResponse,
    RegisterRequest,
    SentenceOut,
    SourceOut,
    TokenResponse,
    UserOut,
)
from src.auth import audit, service
from src.auth.principal import Principal
from src.auth.tokens import issue_access_token
from src.core import logs, metrics, observability, otel
from src.core.config import SETTINGS
from src.core.errors import (
    AuthError,
    DocumentError,
    DocumentNotFound,
    GenerationError,
    ModelUnavailable,
    PermissionDenied,
)
from src.core.health import check_health
from src.generation.answerer import Answer, answer_question
from src.ingestion import jobs
from src.ingestion.parsers import validate_upload
from src.ingestion.pipeline import delete, doc_id_for, list_indexed, scope_doc_ids
from src.storage import objects

observability.setup("api")
logger = logs.logger(__name__)

# Probes and scrapes run every few seconds and carry no query: a span each would be noise.
_UNTRACED_PATHS = frozenset({"/livez", "/metrics"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await jobs.close_pool()
    observability.shutdown()


app = FastAPI(title="Smart Document Assistant", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _observe_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """One server span, one metric pair and one correlation id per request."""
    if request.url.path in _UNTRACED_PATHS:
        return await call_next(request)

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    # A gateway's traceparent is honoured, so the API span joins the caller's trace.
    with otel.tracer().start_as_current_span(
        request.method, context=extract(dict(request.headers)), kind=SpanKind.SERVER
    ) as span:
        logs.bind(request_id=request_id)
        span.set_attribute("sda.request_id", request_id)
        span.set_attribute("http.request.method", request.method)
        try:
            response = await call_next(request)
        except Exception:
            _record_request(span, request, "500", time.perf_counter() - started)
            raise
        finally:
            logs.unbind("request_id")
        _record_request(span, request, str(response.status_code), time.perf_counter() - started)
        response.headers["X-Request-ID"] = request_id
        return response


def _record_request(span, request: Request, status: str, elapsed: float) -> None:
    # The route template, not the URL: /jobs/{job_id} is one series, not one per job. Only
    # available after routing, hence the rename here rather than at span start. A streaming
    # response is already dispatched at this point, so its body time is not in `elapsed`.
    route = getattr(request.scope.get("route"), "path", "unmatched")
    span.update_name(f"{request.method} {route}")
    span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status)
    metrics.HTTP_REQUESTS.labels(request.method, route, status).inc()
    metrics.HTTP_DURATION.labels(request.method, route).observe(elapsed)


@app.exception_handler(AuthError)
def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": exc.message},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(PermissionDenied)
def _permission_denied(request: Request, exc: PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": exc.message})


@app.exception_handler(DocumentNotFound)
def _document_not_found(request: Request, exc: DocumentNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": exc.message})


# --- auth ---


@app.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest) -> TokenResponse:
    """Sign up: creates a tenant and its first user, who is that tenant's admin."""
    principal = service.register_tenant(body.tenant_name, body.email, body.password)
    audit.record(principal, "register", tenant_name=body.tenant_name)
    return _token(principal, body.tenant_name)


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    principal = service.authenticate(body.email, body.password)
    audit.record(principal, "login")
    return _token(principal, service.tenant_name(principal.tenant_id))


@app.get("/auth/me", response_model=UserOut)
def me(principal: Principal = Depends(deps.current_principal)) -> UserOut:
    return _user_out(principal, service.tenant_name(principal.tenant_id))


@app.get("/auth/users", response_model=list[UserOut])
def users(principal: Principal = Depends(deps.require_admin)) -> list[UserOut]:
    name = service.tenant_name(principal.tenant_id)
    return [
        UserOut(
            user_id=u["user_id"],
            email=u["email"],
            role=u["role"],
            tenant_id=principal.tenant_id,
            tenant_name=name,
        )
        for u in service.list_users(principal)
    ]


@app.post("/auth/users", response_model=UserOut, status_code=201)
def create_user(
    body: CreateUserRequest, principal: Principal = Depends(deps.require_admin)
) -> UserOut:
    """Adds a user to the caller's own tenant. The tenant is never a request parameter."""
    created = service.create_user(principal, body.email, body.password, body.role)
    audit.record(principal, "create_user", email=created.email, role=created.role)
    return _user_out(created, service.tenant_name(principal.tenant_id))


@app.get("/audit", response_model=list[AuditEventOut])
def audit_log(principal: Principal = Depends(deps.require_admin)) -> list[AuditEventOut]:
    return [AuditEventOut(**event) for event in audit.read(principal)]


def _token(principal: Principal, tenant_name: str) -> TokenResponse:
    token, ttl = issue_access_token(principal)
    return TokenResponse(
        access_token=token, expires_in=ttl, user=_user_out(principal, tenant_name)
    )


def _user_out(principal: Principal, tenant_name: str) -> UserOut:
    return UserOut(
        user_id=principal.user_id,
        email=principal.email,
        role=principal.role,
        tenant_id=principal.tenant_id,
        tenant_name=tenant_name,
    )


# --- service ---


@app.get("/livez")
def livez() -> dict[str, str]:
    """Liveness: the process is running. Never touches a backend."""
    return {"status": "ok"}


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(**check_health())


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    """Scrape target. Carries no tenant data, so it is bearer-protected only when a token is set."""
    token = SETTINGS.observability.metrics_token
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid metrics token.")
    depth = await anyio.to_thread.run_sync(jobs.queue_depth)
    if depth is not None:
        metrics.QUEUE_DEPTH.set(depth)
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return ConfigResponse(
        retrieval_modes=list(SETTINGS.retrieval.modes),
        default_mode=SETTINGS.retrieval.mode,
        max_question_chars=SETTINGS.max_question_chars,
    )


@app.get("/documents", response_model=list[DocumentOut])
def documents(principal: Principal = Depends(deps.require_viewer)) -> list[DocumentOut]:
    return [DocumentOut(**doc) for doc in list_indexed(principal.tenant_id)]


@app.post("/ingest", response_model=JobOut, status_code=202)
async def ingest_document(
    response: Response,
    file: UploadFile = File(...),
    principal: Principal = Depends(deps.require_editor),
) -> JobOut:
    """Stage the upload and queue it. Parsing and embedding happen in the ingest worker."""
    data = await file.read()
    filename = file.filename or "upload"
    try:
        validate_upload(filename, data)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    doc_id = doc_id_for(principal.tenant_id, data)
    job_id = jobs.job_id_for(doc_id)

    # Idempotency key is the tenant-salted content hash: the same bytes in flight are one
    # job, and two tenants uploading the same file do not collide on it.
    holder = await anyio.to_thread.run_sync(jobs.claim, doc_id, job_id)
    if holder is not None:
        state = await anyio.to_thread.run_sync(jobs.get, holder, principal.tenant_id)
        if state is not None:
            response.status_code = 200
            return JobOut(**state)

    await anyio.to_thread.run_sync(jobs.record_queued, job_id, doc_id, filename, principal)
    await anyio.to_thread.run_sync(objects.put_raw, doc_id, filename, data)
    await _enqueue(job_id, doc_id, filename, principal)
    return JobOut(job_id=job_id, doc_id=doc_id, filename=filename, status=jobs.QUEUED, stage="Queued")


async def _enqueue(
    job_id: str, doc_id: str, filename: str, owner: Principal, attempt: int = 0
) -> None:
    """Queue the work. Job state lives under job_id; arq's own id changes between attempts,
    because arq keeps the key of a finished job and would silently drop a re-run."""
    arq_id = job_id if attempt == 0 else f"{job_id}:{uuid.uuid4().hex[:8]}"
    enqueued = await (await jobs.pool()).enqueue_job(
        "ingest_job",
        doc_id,
        filename,
        job_id,
        asdict(owner),
        _job_id=arq_id,
        _queue_name=SETTINGS.jobs.queue_name,
    )
    if enqueued is None:
        await _enqueue(job_id, doc_id, filename, owner, attempt + 1)


@app.get("/jobs/dead-letters")
async def dead_letters(principal: Principal = Depends(deps.require_admin)) -> list[dict]:
    """Poison documents in the caller's tenant: jobs that failed terminally, newest first."""
    return await anyio.to_thread.run_sync(jobs.dead_letters, principal.tenant_id)


@app.get("/jobs/{job_id}", response_model=JobOut)
async def job_status(
    job_id: str, principal: Principal = Depends(deps.require_viewer)
) -> JobOut:
    state = await anyio.to_thread.run_sync(jobs.get, job_id, principal.tenant_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return JobOut(**state)


@app.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str, request: Request, principal: Principal = Depends(deps.require_viewer)
) -> StreamingResponse:
    return StreamingResponse(
        _stream_job(job_id, request, principal.tenant_id), media_type="text/event-stream"
    )


async def _stream_job(job_id: str, request: Request, tenant_id: str) -> AsyncIterator[str]:
    """Poll the job's Redis state and push every change. Ends on terminal state or disconnect."""
    deadline = asyncio.get_running_loop().time() + SETTINGS.jobs.progress_timeout_s
    last: tuple[str, str] = ("", "")
    while True:
        if await request.is_disconnected():
            return
        state = await anyio.to_thread.run_sync(jobs.get, job_id, tenant_id)
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
def delete_document(
    doc_id: str, principal: Principal = Depends(deps.require_editor)
) -> dict[str, str]:
    delete(doc_id, principal)
    audit.record(principal, "delete", doc_id=doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@app.post("/query")
def query(
    request: QueryRequest, principal: Principal = Depends(deps.require_viewer)
) -> StreamingResponse:
    _validate_query(request)
    # Narrowed at the boundary as well as in the retrieval predicates: a doc_id from
    # another tenant is dropped here and would match nothing even if it were not.
    doc_ids = scope_doc_ids(request.doc_ids, principal.tenant_id)
    audit.record(
        principal, "query", num_docs=len(doc_ids), mode=request.mode, chars=len(request.question)
    )
    return StreamingResponse(
        _stream_answer(request, doc_ids, principal), media_type="text/event-stream"
    )


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


def _stream_answer(request: QueryRequest, doc_ids: list[str], principal: Principal):
    events: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def on_token(text: str) -> None:
        events.put(("token", {"text": text}))

    def run() -> None:
        try:
            answer = answer_question(
                request.question,
                doc_ids,
                deps.embedder(),
                deps.vector_store(),
                deps.llm_client(),
                keyword_index=deps.keyword_index(),
                reranker=deps.reranker(),
                principal=principal,
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

    # The answer runs off the event loop, and a bare thread starts with an empty context: the
    # copy carries the request's span and its log bindings into the stage spans.
    context = contextvars.copy_context()
    threading.Thread(target=lambda: context.run(run), daemon=True).start()

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
