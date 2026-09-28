from typing import Any, Optional

from pydantic import BaseModel, Field

from src.core.config import SETTINGS


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=SETTINGS.max_question_chars)
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = SETTINGS.retrieval.mode
    generate: bool = True
    history: list[tuple[str, str]] = Field(default_factory=list)
    # False answers in one JSON response instead of an SSE stream. Tokens are still generated
    # the same way; the difference is only whether the caller sees them arrive.
    stream: bool = True


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=SETTINGS.auth.min_password_chars, max_length=256)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=SETTINGS.auth.min_password_chars, max_length=256)
    role: str = "viewer"


class UserOut(BaseModel):
    user_id: str
    email: str
    role: str
    tenant_id: str
    tenant_name: str


class ErasureReceipt(BaseModel):
    """Proof of a right-to-erasure request. `complete` is the verification pass, not a claim:
    it is false unless a re-read of every store found nothing left."""

    subject_user_id: str
    tenant_id: str
    erased_at: str
    doc_ids: list[str]
    deleted: dict[str, int]
    redacted: dict[str, int]
    remaining: dict[str, int]
    retained: dict[str, str]
    complete: bool
    digest: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class AuditEventOut(BaseModel):
    event_id: int
    email: str
    action: str
    doc_id: Optional[str]
    detail: dict[str, Any]
    created_at: str


class SourceOut(BaseModel):
    id: int
    filename: str
    pages: str
    section_path: str
    text: str
    char_span_in_parent: list[int] = Field(default_factory=lambda: [0, 0])
    score: float = 0.0


class SentenceOut(BaseModel):
    text: str
    cites: list[int]
    citation_status: str


class ConflictOut(BaseModel):
    claim: str
    source_ids: list[int]


class ConfidenceOut(BaseModel):
    label: str
    score: float
    components: dict[str, float]


class QueryResponse(BaseModel):
    query_id: str
    status: str
    answer_text: str
    sentences: list[SentenceOut]
    sources: list[SourceOut]
    conflicts: list[ConflictOut]
    confidence: Optional[ConfidenceOut]
    abstain_reason: Optional[str]
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    near_miss: Optional[dict[str, Any]] = None
    trace: dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    doc_id: str
    filename: str
    pages: int
    num_parents: int
    num_children: int
    outcome: str


class JobOut(BaseModel):
    job_id: str
    doc_id: str
    filename: str
    status: str
    stage: str
    error: Optional[str] = None
    report: Optional[IngestResponse] = None


class DocumentOut(BaseModel):
    doc_id: str
    filename: str
    pages: int
    num_children: int
    owner_id: str


class DocumentPage(BaseModel):
    """`next_cursor` is None on the last page. It is opaque: pass it back, do not parse it."""

    items: list[DocumentOut]
    next_cursor: Optional[str] = None


class DocumentVersionOut(BaseModel):
    doc_id: str
    version: int
    pages: int
    num_children: int
    created_at: str
    superseded_by: Optional[str]
    deleted_at: Optional[str]
    current: bool


class IndexStatusOut(BaseModel):
    """`reindex_needed` true means the running configuration would build a different index from
    the one being read: a chunker or embedder change that has not been rolled out yet."""

    active_version: Optional[str]
    previous_version: Optional[str]
    building_version: str
    reindex_needed: bool
    updated_at: Optional[str]
    progress: dict[str, Any]


class WebhookRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(default_factory=list)


class WebhookOut(BaseModel):
    webhook_id: str
    url: str
    events: list[str]
    active: bool
    consecutive_failures: int
    last_error: Optional[str]
    last_delivery_at: Optional[str]
    created_at: Optional[str]


class WebhookCreated(WebhookOut):
    """The only response that carries the secret. It is not readable again."""

    secret: str


class BulkIngestOut(BaseModel):
    """One entry per file, in request order. A rejected file carries its reason and no job."""

    accepted: list[JobOut]
    rejected: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    provider: str
    postgres: str
    redis: str
    object_store: str
    embedder: str
    reranker: str
    llm: str


class ConfigResponse(BaseModel):
    retrieval_modes: list[str]
    default_mode: str
    max_question_chars: int
