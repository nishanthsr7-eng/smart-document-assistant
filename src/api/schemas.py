from typing import Any, Optional

from pydantic import BaseModel, Field

from src.core.config import SETTINGS


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=SETTINGS.max_question_chars)
    doc_ids: list[str] = Field(default_factory=list)
    mode: str = SETTINGS.retrieval.mode
    generate: bool = True
    history: list[tuple[str, str]] = Field(default_factory=list)


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
