import asyncio
import contextlib
import contextvars
import json
import queue
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Optional

import anyio
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind

from src.api import conditional, deps, idempotency, pagination, problems, webhooks
from src.api.schemas import (
    AuditEventOut,
    BulkIngestOut,
    ConfidenceOut,
    ConfigResponse,
    ConflictOut,
    CreateUserRequest,
    DocumentOut,
    DocumentPage,
    DocumentVersionOut,
    ErasureReceipt,
    HealthResponse,
    IndexStatusOut,
    JobOut,
    LoginRequest,
    QueryRequest,
    QueryResponse,
    RegisterRequest,
    SentenceOut,
    SourceOut,
    TokenResponse,
    UserOut,
    WebhookCreated,
    WebhookOut,
    WebhookRequest,
)
from src.auth import audit, service
from src.auth.principal import Principal
from src.auth.tokens import issue_access_token
from src.core import limits, logs, metrics, observability, otel
from src.core.config import SETTINGS
from src.core.errors import (
    DocumentError,
    DocumentTooLarge,
    GenerationError,
    ModelUnavailable,
    QueryCancelled,
)
from src.core.health import check_health, draining, start_draining
from src.generation.answerer import Answer, answer_question
from src.ingestion import jobs, reindex, upload
from src.ingestion.pipeline import (
    delete,
    doc_id_for_stream,
    list_indexed,
    pending_work,
    restore,
    scope_doc_ids,
    versions,
)
from src.storage import alias, erasure, objects

observability.setup("api")
logger = logs.logger(__name__)

# Probes and scrapes run every few seconds and carry no query: a span each would be noise.
_UNTRACED_PATHS = frozenset({"/livez", "/metrics"})
# The upload cap plus a megabyte for multipart framing: the request is larger than the file.
_MAX_REQUEST_BYTES = (SETTINGS.ingestion.max_upload_mb + 1) * 1024 * 1024
# One thread per in-flight query, held for its whole life: reranking and generation both block.
# The semaphore is what the endpoint tests to shed load, so it must match the pool exactly --
# submitting past the pool's width would queue silently instead of answering 503.
_QUERY_POOL = ThreadPoolExecutor(
    max_workers=SETTINGS.api.query_concurrency, thread_name_prefix="query"
)
_QUERY_SLOTS = threading.Semaphore(SETTINGS.api.query_concurrency)
# An SSE comment: ignored by the client, enough to keep a proxy from timing out a quiet stream.
_KEEPALIVE = ": keep-alive\n\n"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _drain_on_signal()
    yield
    # By the time this runs, uvicorn has stopped accepting and has already awaited the
    # connections it had -- including the SSE answers, which is why its
    # --timeout-graceful-shutdown must exceed QUERY_TIMEOUT_S. This is the last step: drain the
    # pool so any thread still finishing an answer does, rather than dying with the process.
    _QUERY_POOL.shutdown(wait=True)
    await jobs.close_pool()
    observability.shutdown()


def _drain_on_signal() -> None:
    """Fail readiness the instant a shutdown starts, not when it finishes.

    Graceful shutdown of a streaming API is a race between two things kubernetes does at once:
    it sends SIGTERM, and it removes the pod from the service endpoints. If SIGTERM wins, this
    replica stops accepting while the balancer is still routing to it, and those requests are
    refused connections rather than answers. The chart's preStop sleep is what gives endpoint
    removal a head start; this handler is the other half, so a readiness probe arriving during
    that window is told the truth immediately instead of up to ten seconds later.

    Uvicorn's own handler is kept and called after -- replacing it would mean the process never
    shuts down.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: object, _previous: Any = previous) -> None:
            start_draining()
            logger.info("Draining", extra={"signal": signum})
            if callable(_previous):
                _previous(signum, frame)

        signal.signal(sig, handler)


app = FastAPI(title="Smart Document Assistant", version="1.0", lifespan=lifespan)

# Everything a consumer calls hangs off this router and is mounted twice: once under /v1, which
# is the contract, and once unversioned for the paths that already exist. The unversioned mount
# is out of the schema, so /openapi.json describes one API rather than two.
api = APIRouter()
# Probes and the scrape target are infrastructure, not the product API: a load balancer's health
# check should not have to know which version of the contract is deployed, so they stay off it.

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(SETTINGS.security.cors_allow_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The API serves JSON and SSE, never markup, so the policy can be the strict one: nothing loads,
# nothing frames it, and a response that a browser is tricked into rendering can do nothing.
_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
    f"connect-src {SETTINGS.security.csp_connect_src}"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # This API has no use for any of them, and a stale embed or extension should not either.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
}
if SETTINGS.security.hsts_max_age_s:
    _SECURITY_HEADERS["Strict-Transport-Security"] = (
        f"max-age={SETTINGS.security.hsts_max_age_s}; includeSubDomains"
    )


@app.middleware("http")
async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def _cap_request_body(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse an over-long body before anything parses it.

    The per-chunk cap in `upload.receive` bounds what this process holds, but Starlette's
    multipart parser has already spooled the body by the time the handler runs. A declared
    length is the only chance to reject a 2 GB POST without touching it at all. An undeclared
    (chunked) body still falls through to the streaming cap, and to the proxy's own limit.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_REQUEST_BYTES:
        return problems.problem(
            request,
            413,
            "payload-too-large",
            "Payload too large",
            DocumentTooLarge(SETTINGS.ingestion.max_upload_mb).message,
        )
    return await call_next(request)


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


# Every error leaves as RFC 9457 problem+json, including the ones FastAPI raises itself, so a
# client writes one parser rather than one per failure mode.
for _exception, _handler in problems.HANDLERS:
    app.add_exception_handler(_exception, _handler)


# --- auth ---


@api.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, request: Request) -> TokenResponse:
    """Sign up: creates a tenant and its first user, who is that tenant's admin."""
    limits.check_rate("auth", _client_ip(request))
    principal = service.register_tenant(body.tenant_name, body.email, body.password)
    audit.record(principal, "register", tenant_name=body.tenant_name)
    return _token(principal, body.tenant_name)


@api.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    limits.check_rate("auth", _client_ip(request))
    principal = service.authenticate(body.email, body.password)
    audit.record(principal, "login")
    return _token(principal, service.tenant_name(principal.tenant_id))


@api.get("/auth/me", response_model=UserOut)
def me(principal: Principal = Depends(deps.current_principal)) -> UserOut:
    return _user_out(principal, service.tenant_name(principal.tenant_id))


@api.get("/auth/users", response_model=list[UserOut])
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


@api.post("/auth/users", response_model=UserOut, status_code=201)
def create_user(
    body: CreateUserRequest, principal: Principal = Depends(deps.require_admin)
) -> UserOut:
    """Adds a user to the caller's own tenant. The tenant is never a request parameter."""
    created = service.create_user(principal, body.email, body.password, body.role)
    audit.record(principal, "create_user", email=created.email, role=created.role)
    return _user_out(created, service.tenant_name(principal.tenant_id))


@api.delete("/auth/users/{user_id}/data", response_model=ErasureReceipt)
def erase_user_data(
    user_id: str, principal: Principal = Depends(deps.require_admin)
) -> ErasureReceipt:
    """Right to erasure for one data subject, within the caller's own tenant.

    Returns a receipt rather than 204: the requester is entitled to know what was removed, and
    `complete` comes from a re-read of every store, so a partial erasure reports itself.
    """
    receipt = erasure.erase_user(principal, user_id)
    audit.record(
        principal,
        "erase_user",
        subject=user_id,
        documents=receipt.deleted["documents"],
        complete=receipt.complete,
        digest=receipt.digest(),
    )
    return ErasureReceipt(**asdict(receipt), complete=receipt.complete, digest=receipt.digest())


@api.get("/audit", response_model=list[AuditEventOut])
def audit_log(principal: Principal = Depends(deps.require_admin)) -> list[AuditEventOut]:
    return [AuditEventOut(**event) for event in audit.read(principal)]


def _client_ip(request: Request) -> str:
    """The caller's address for the pre-auth limiter. X-Forwarded-For's first hop is trusted
    because the deployment always terminates at an ingress that rewrites it; direct exposure
    would let a caller spoof the header and get a fresh bucket per request."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
def health(response: Response) -> HealthResponse:
    """Readiness. 503 while draining, so a load balancer removes this replica before it stops
    accepting connections rather than after."""
    checks = check_health()
    if checks["status"] != "ok":
        response.status_code = 503
    return HealthResponse(**checks)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    """Scrape target. Carries no tenant data, so it is bearer-protected only when a token is set."""
    token = SETTINGS.observability.metrics_token
    if token and request.headers.get("authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid metrics token.")
    depth = await anyio.to_thread.run_sync(jobs.queue_depth)
    if depth is not None:
        metrics.QUEUE_DEPTH.set(depth)
    # Backlogs rather than counters: "the job has not run" and "the job had nothing to do" look
    # identical in a counter, and only one of them is a problem.
    backlog = await anyio.to_thread.run_sync(pending_work)
    metrics.REINDEX_PENDING.set(backlog["reindex_pending"])
    metrics.RETENTION_OVERDUE.set(backlog["retention_overdue"])
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@api.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return ConfigResponse(
        retrieval_modes=list(SETTINGS.retrieval.modes),
        default_mode=SETTINGS.retrieval.mode,
        max_question_chars=SETTINGS.max_question_chars,
    )


@api.get("/documents", response_model=DocumentPage)
def documents(
    request: Request,
    response: Response,
    limit: int = Query(default=SETTINGS.api.page_size, ge=1),
    cursor: Optional[str] = Query(default=None),
    principal: Principal = Depends(deps.require_viewer),
) -> object:
    """One page of the tenant's live documents, oldest first.

    Over-asking is capped rather than refused: a client that wants everything follows cursors.
    The page is ETagged because the SPA re-reads it after every ingest and on every reconnect,
    and almost all of those reads are of a list that has not changed.
    """
    size = min(limit, SETTINGS.api.max_page_size)
    # One extra row is the whole "is there a next page" test: a cursor is issued only when a
    # row exists beyond this page, so a client never follows one to an empty result.
    rows = list_indexed(
        principal.tenant_id, limit=size + 1, after=pagination.decode_optional(cursor)
    )
    has_more = len(rows) > size
    rows = rows[:size]
    page = DocumentPage(
        items=[
            DocumentOut(
                doc_id=row["doc_id"],
                filename=row["filename"],
                pages=row["pages"],
                num_children=row["num_children"],
                owner_id=row["owner_id"],
            )
            for row in rows
        ],
        next_cursor=(
            pagination.encode(rows[-1]["created_at"], rows[-1]["doc_id"]) if has_more else None
        ),
    )
    return conditional.conditional(request, response, page.model_dump()) or page


@api.post("/ingest", response_model=JobOut, status_code=202)
async def ingest_document(
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(deps.require_editor),
) -> JobOut:
    """Stage the upload and queue it. Parsing and embedding happen in the ingest worker.

    The body is never read whole: it is spooled in chunks under the size cap, so an oversized
    POST is refused as it arrives rather than after it is resident.
    """
    limits.check_rate("ingest", principal.tenant_id)
    status, job = await _accept_upload(file, principal, idempotency_key)
    response.status_code = status
    return job


@api.post("/ingest/batch", response_model=BulkIngestOut, status_code=202)
async def ingest_batch(
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(deps.require_editor),
) -> BulkIngestOut:
    """Stage several uploads in one request. Each still becomes its own job.

    One bad file does not fail the batch: a rejection is reported per file, in request order,
    so a caller uploading a folder learns which document it has to fix rather than which
    request to repeat. The rate limit is charged per file, because each is a real ingest.
    """
    if len(files) > SETTINGS.api.max_bulk_files:
        raise HTTPException(
            status_code=422,
            detail=f"At most {SETTINGS.api.max_bulk_files} files per batch.",
        )
    accepted: list[JobOut] = []
    rejected: list[dict] = []
    for file in files:
        limits.check_rate("ingest", principal.tenant_id)
        try:
            _, job = await _accept_upload(file, principal, idempotency_key=None)
        except (DocumentError, HTTPException) as exc:
            detail = exc.message if isinstance(exc, DocumentError) else exc.detail
            rejected.append({"filename": file.filename or "upload", "detail": detail})
            continue
        accepted.append(job)
    return BulkIngestOut(accepted=accepted, rejected=rejected)


async def _accept_upload(
    file: UploadFile, principal: Principal, idempotency_key: Optional[str]
) -> tuple[int, JobOut]:
    """Stage one upload and queue its job. Returns the status the caller should see: 202 for
    work that was created, 200 for work that already existed."""
    filename = file.filename or "upload"
    staged = await upload.receive(filename, _body_chunks(file))
    try:
        doc_id = await anyio.to_thread.run_sync(
            doc_id_for_stream, principal.tenant_id, staged.body
        )
        job_id = jobs.job_id_for(doc_id)

        # The Idempotency-Key check needs the content hash, so it happens after staging rather
        # than before: staging is a spooled copy and no job, which is why running it for a
        # request that turns out to be a replay is harmless. What the key catches is the case
        # the content hash cannot see -- the same key sent with a *different* file.
        claimed = None
        if idempotency_key is not None:
            replay = await anyio.to_thread.run_sync(
                idempotency.begin, principal.tenant_id, "ingest", idempotency_key, doc_id
            )
            if replay is not None:
                return replay.status, JobOut(**replay.body)
            claimed = idempotency_key

        try:
            status, job = await _queue_upload(job_id, doc_id, filename, staged, principal)
        except BaseException:
            if claimed is not None:
                await anyio.to_thread.run_sync(
                    idempotency.abandon, principal.tenant_id, "ingest", claimed
                )
            raise
        if claimed is not None:
            await anyio.to_thread.run_sync(
                idempotency.complete,
                principal.tenant_id,
                "ingest",
                claimed,
                doc_id,
                status,
                job.model_dump(),
            )
        return status, job
    finally:
        staged.close()


async def _queue_upload(
    job_id: str, doc_id: str, filename: str, staged: upload.StagedUpload, principal: Principal
) -> tuple[int, JobOut]:
    # The content hash is the other idempotency key, and the one that needs no client
    # cooperation: the same bytes in flight are one job, and two tenants uploading the same
    # file do not collide on it.
    holder = await anyio.to_thread.run_sync(jobs.claim, doc_id, job_id)
    if holder is not None:
        state = await anyio.to_thread.run_sync(jobs.get, holder, principal.tenant_id)
        if state is not None:
            return 200, JobOut(**state)

    await anyio.to_thread.run_sync(jobs.record_queued, job_id, doc_id, filename, principal)
    await anyio.to_thread.run_sync(objects.put_raw, doc_id, filename, staged.body)
    await _enqueue(job_id, doc_id, filename, principal)
    return 202, JobOut(
        job_id=job_id, doc_id=doc_id, filename=filename, status=jobs.QUEUED, stage="Queued"
    )


async def _body_chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(SETTINGS.ingestion.upload_chunk_bytes):
        yield chunk


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


@api.get("/jobs/dead-letters")
async def dead_letters(principal: Principal = Depends(deps.require_admin)) -> list[dict]:
    """Poison documents in the caller's tenant: jobs that failed terminally, newest first."""
    return await anyio.to_thread.run_sync(jobs.dead_letters, principal.tenant_id)


@api.get("/jobs/{job_id}", response_model=JobOut)
async def job_status(
    job_id: str, principal: Principal = Depends(deps.require_viewer)
) -> JobOut:
    state = await anyio.to_thread.run_sync(jobs.get, job_id, principal.tenant_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return JobOut(**state)


@api.get("/jobs/{job_id}/events")
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


@api.delete("/documents/{doc_id}")
def delete_document(
    doc_id: str, principal: Principal = Depends(deps.require_editor)
) -> dict[str, str]:
    delete(doc_id, principal)
    audit.record(principal, "delete", doc_id=doc_id)
    return {"status": "deleted", "doc_id": doc_id}


@api.post("/query")
async def query(
    request: QueryRequest, principal: Principal = Depends(deps.require_viewer)
) -> Response:
    _validate_query(request)
    if draining():
        raise HTTPException(
            status_code=503,
            detail="This replica is shutting down. Retry.",
            headers={"Retry-After": "1", "Connection": "close"},
        )
    # Limits before load shedding: a tenant that is out of rate or out of budget is refused for
    # a reason of its own, and telling it the service is busy instead would be a lie.
    limits.check_rate("query", principal.tenant_id)
    if request.generate:
        limits.check_budget(principal.tenant_id)
    # Shed before doing any work: a query holds a worker thread for its whole life, so past the
    # cap the honest answer is 503 now rather than a slot queued behind a saturated reranker.
    if not _QUERY_SLOTS.acquire(blocking=False):
        metrics.QUERIES_ABANDONED.labels("shed").inc()
        raise HTTPException(
            status_code=503,
            detail="The service is at capacity. Retry shortly.",
            headers={"Retry-After": "5"},
        )
    try:
        # Narrowed at the boundary as well as in the retrieval predicates: a doc_id from
        # another tenant is dropped here and would match nothing even if it were not.
        doc_ids = scope_doc_ids(request.doc_ids, principal.tenant_id)
        audit.record(
            principal, "query", num_docs=len(doc_ids), mode=request.mode, chars=len(request.question)
        )
    except BaseException:
        _QUERY_SLOTS.release()
        raise
    # From here the slot belongs to the generator, which releases it on every exit -- which is
    # also why the JSON variant must drain it rather than abandon it part way.
    events = _answer_events(request, doc_ids, principal)
    if not request.stream:
        return await _collect_answer(events)
    return StreamingResponse(_as_sse(events), media_type="text/event-stream")


async def _as_sse(events: AsyncGenerator[tuple[str, dict], None]) -> AsyncIterator[str]:
    """Render the event stream as SSE. The keepalive is a comment frame, and an error's
    `status` is an internal hint for the JSON variant rather than part of the wire contract.

    The inner generator is closed explicitly: `async for` does not close it when this one is
    closed, and it owns the cancel flag and the worker slot -- leaving it to the garbage
    collector would mean a closed browser tab keeps billing until then.
    """
    try:
        async for event, data in events:
            if event == "keepalive":
                yield _KEEPALIVE
                continue
            if event == "error":
                data = {k: v for k, v in data.items() if k != "status"}
            yield _sse(event, data)
    finally:
        await events.aclose()


async def _collect_answer(events: AsyncGenerator[tuple[str, dict], None]) -> Response:
    """The same answer, delivered once. Tokens are still generated and still cancellable; the
    only difference is that a programmatic caller gets one JSON body instead of a stream."""
    terminal: Optional[tuple[str, dict]] = None
    try:
        # Drained to exhaustion rather than returned from on the first terminal event. Returning
        # early leaves the generator suspended on the frame it just yielded, and closing it from
        # there is indistinguishable, inside the generator, from the client having gone away --
        # so every non-streaming answer was recorded as an abandoned query.
        async for event, data in events:
            if event in ("done", "error") and terminal is None:
                terminal = (event, data)
    finally:
        await events.aclose()

    if terminal is not None and terminal[0] == "done":
        return JSONResponse(content=terminal[1])
    detail = terminal[1]["detail"] if terminal else "The answer ended early."
    status = terminal[1].get("status", 502) if terminal else 502
    return problems.problem(
        None, status, "generation-failed", "Answer could not be produced", detail
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


async def _answer_events(
    request: QueryRequest, doc_ids: list[str], principal: Principal
) -> AsyncGenerator[tuple[str, dict], None]:
    """Own one worker slot for the life of the stream, and give it up on every exit.

    Two ways a query ends without an answer, and both have to reach the answering thread: the
    client goes away, or the deadline passes. The thread is not killable, so `cancel` is a flag it
    checks at every stage boundary and every token -- which is what stops a closed browser tab
    from streaming a provider bill to completion.

    A disconnect arrives as `GeneratorExit`: Starlette already races the body stream against a
    disconnect listener and closes this generator when the socket goes. Polling
    `request.is_disconnected()` here as well was measured to never win that race, and it reads the
    same receive channel the listener is waiting on -- so the `finally` is the whole mechanism.
    """
    # Bounded: a client slower than the model applies backpressure instead of letting the queue
    # grow without limit.
    events: "queue.Queue[tuple[str, dict]]" = queue.Queue(
        maxsize=SETTINGS.api.stream_queue_size
    )
    cancel = threading.Event()
    poll = SETTINGS.api.stream_poll_s

    def emit(event: str, data: dict) -> None:
        while not cancel.is_set():
            try:
                events.put((event, data), timeout=poll)
                return
            except queue.Full:
                continue

    def on_token(text: str) -> None:
        emit("token", {"text": text})

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
                cancel=cancel.is_set,
            )
            emit("done", _to_response(answer).model_dump())
        except QueryCancelled as exc:
            logger.info("Query cancelled", extra={"stage": exc.reason})
        except (ModelUnavailable, GenerationError) as exc:
            emit("error", {"detail": str(exc)})
        except Exception:
            logger.exception("Unhandled error while streaming answer")
            emit("error", {"detail": "Internal server error."})
        finally:
            # Straight to the queue, not through emit: the end marker has to land even when the
            # consumer is gone and emit's loop would refuse to block for it.
            with contextlib.suppress(queue.Full):
                events.put_nowait(("__end__", {}))

    # The answer runs off the event loop, and a bare thread starts with an empty context: the
    # copy carries the request's span and its log bindings into the stage spans. The pool is
    # bounded, but the slot was already taken in the endpoint, so submitting cannot block here.
    context = contextvars.copy_context()
    metrics.QUERIES_IN_FLIGHT.inc()
    _QUERY_POOL.submit(context.run, run)
    deadline = time.monotonic() + SETTINGS.api.query_timeout_s
    last_sent = time.monotonic()
    ended = False
    try:
        while True:
            now = time.monotonic()
            if now > deadline:
                _abandon(cancel, "timeout")
                ended = True
                yield (
                    "error",
                    {
                        "detail": "The query took too long and was cancelled.",
                        "status": 504,
                    },
                )
                return
            try:
                event, data = await anyio.to_thread.run_sync(_next_event, events, poll)
            except queue.Empty:
                # A stage can run for tens of seconds with nothing to send. The comment frame
                # keeps the connection alive through a proxy and costs one line.
                if now - last_sent >= SETTINGS.api.stream_keepalive_s:
                    last_sent = now
                    yield ("keepalive", {})
                continue
            if event == "__end__":
                ended = True
                return
            last_sent = now
            yield (event, data)
    finally:
        # Reached on every exit, including the GeneratorExit a disconnect raises. `ended` is the
        # only thing that separates "the answer finished" from "nobody is listening any more".
        if not ended:
            _abandon(cancel, "disconnect")
        metrics.QUERIES_IN_FLIGHT.dec()
        _QUERY_SLOTS.release()


def _next_event(events: "queue.Queue[tuple[str, dict]]", poll: float) -> tuple[str, dict]:
    return events.get(timeout=poll)


def _abandon(cancel: threading.Event, reason: str) -> None:
    cancel.set()
    metrics.QUERIES_ABANDONED.labels(reason).inc()
    logger.info("Query abandoned", extra={"reason": reason})


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


# --- document lifecycle ---


@api.get("/documents/{doc_id}/versions", response_model=list[DocumentVersionOut])
def document_versions(
    doc_id: str, principal: Principal = Depends(deps.require_viewer)
) -> list[DocumentVersionOut]:
    """Every upload of this document's filename, newest first. Replacing a file by name keeps
    what it replaced for the retention window, so this is a history rather than one row."""
    return [DocumentVersionOut(**row) for row in versions(doc_id, principal.tenant_id)]


@api.post("/documents/{doc_id}/restore", response_model=JobOut, status_code=202)
async def restore_document(
    doc_id: str, principal: Principal = Depends(deps.require_editor)
) -> JobOut:
    """Undo a soft delete, inside the window. The bytes survived; the index did not, so this
    returns an ingest job rather than a document that is instantly answerable again."""
    document = restore(doc_id, principal)
    job_id = jobs.job_id_for(doc_id)
    await anyio.to_thread.run_sync(
        jobs.record_queued, job_id, doc_id, document["filename"], principal
    )
    await _enqueue(job_id, doc_id, document["filename"], principal)
    audit.record(principal, "restore", doc_id=doc_id)
    return JobOut(
        job_id=job_id,
        doc_id=doc_id,
        filename=document["filename"],
        status=jobs.QUEUED,
        stage="Queued",
    )


# --- operator: the index build the whole deployment reads from ---


@api.get("/admin/index", response_model=IndexStatusOut)
def index_status(principal: Principal = Depends(deps.require_operator)) -> IndexStatusOut:
    """What is being read, what the running configuration would write, and whether the last
    rebuild finished. `reindex_needed` true means a config change is not live yet."""
    return IndexStatusOut(**reindex.status())


@api.post("/admin/index/reindex", status_code=202)
async def start_reindex(principal: Principal = Depends(deps.require_operator)) -> dict[str, str]:
    """Rebuild every stale document beside the live index, then cut over. Reads are served by
    the current build throughout; poll GET /admin/index for progress."""
    await (await jobs.pool()).enqueue_job(
        "reindex_job", _queue_name=SETTINGS.jobs.queue_name
    )
    audit.record(principal, "reindex", target=SETTINGS.ingest_version)
    return {"status": "queued", "target": SETTINGS.ingest_version}


@api.post("/admin/index/rollback", response_model=IndexStatusOut)
def rollback_index(principal: Principal = Depends(deps.require_operator)) -> IndexStatusOut:
    """Point reads back at the previous build. Possible for as long as its chunks survive
    RETENTION_OLD_INDEX_DAYS -- after that the rollback is another reindex."""
    try:
        target = alias.rollback()
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.record(principal, "index_rollback", target=target)
    return IndexStatusOut(**reindex.status())


@api.post("/admin/retention/sweep", status_code=202)
async def sweep_retention(principal: Principal = Depends(deps.require_operator)) -> dict[str, str]:
    """Run the retention sweep now. It also runs hourly in the worker; this is for a drill."""
    await (await jobs.pool()).enqueue_job(
        "retention_sweep", _queue_name=SETTINGS.jobs.queue_name
    )
    audit.record(principal, "retention_sweep")
    return {"status": "queued"}



# --- webhooks ---


@api.get("/webhooks", response_model=list[WebhookOut])
def list_webhooks(principal: Principal = Depends(deps.require_admin)) -> list[WebhookOut]:
    return [WebhookOut(**hook) for hook in webhooks.listing(principal.tenant_id)]


@api.post("/webhooks", response_model=WebhookCreated, status_code=201)
def create_webhook(
    body: WebhookRequest, principal: Principal = Depends(deps.require_admin)
) -> WebhookCreated:
    """Subscribe to ingest outcomes. The signing secret is in this response and nowhere else."""
    created = webhooks.register(principal.tenant_id, body.url, body.events)
    audit.record(principal, "webhook_create", url=body.url, events=created["events"])
    return WebhookCreated(**created)


@api.delete("/webhooks/{webhook_id}")
def delete_webhook(
    webhook_id: str, principal: Principal = Depends(deps.require_admin)
) -> dict[str, str]:
    webhooks.unregister(principal.tenant_id, webhook_id)
    audit.record(principal, "webhook_delete", webhook_id=webhook_id)
    return {"status": "deleted", "webhook_id": webhook_id}



# Mounted last, after every route above is defined. /v1 is the contract; the unversioned mount
# keeps the paths that shipped before it working, and is the one to retire first.
app.include_router(api, prefix="/v1")
app.include_router(api, include_in_schema=False, deprecated=True)
