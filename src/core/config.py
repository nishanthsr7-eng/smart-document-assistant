import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from src.core import thresholds
from src.core.errors import ConfigError
from src.core.flags import Flags
from src.core.thresholds import TrustThresholds

ROOT_DIR = Path(__file__).resolve().parents[2]

_ConfigT = TypeVar("_ConfigT", bound=BaseModel)

# pydantic-settings reads .env itself, but PROMETHEUS_MULTIPROC_DIR is read straight from the
# process environment by prometheus_client, so .env still has to land in os.environ.
load_dotenv()

_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env", extra="ignore", frozen=True, populate_by_name=True
)


class Paths(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path = ROOT_DIR
    data: Path = ROOT_DIR / "data"
    models: Path = ROOT_DIR / "data" / "models"
    sample_docs: Path = ROOT_DIR / "data" / "sample_docs"


class StorageConfig(BaseSettings):
    """Shared-state backends. All required: the service has no process-local fallback."""

    model_config = _SETTINGS_CONFIG

    database_url: str
    redis_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "us-east-1"
    embedding_dim: int = 768
    pool_size: int = Field(default=5, gt=0, validation_alias="DB_POOL_SIZE")
    pool_max_overflow: int = Field(default=10, ge=0, validation_alias="DB_POOL_MAX_OVERFLOW")
    ingest_lock_ttl_s: int = Field(default=900, gt=0)

    @field_validator("database_url")
    @classmethod
    def _is_a_sqlalchemy_url(cls, value: str) -> str:
        if not value.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a postgresql:// URL -- pgvector is required")
        return value

    @field_validator("redis_url")
    @classmethod
    def _is_a_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return value

    @field_validator("s3_endpoint")
    @classmethod
    def _is_an_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("S3_ENDPOINT must be an http(s) URL")
        return value


class JobsConfig(BaseSettings):
    """Ingest job queue (arq on Redis). Ingest runs in a worker, never on the request path."""

    model_config = _SETTINGS_CONFIG

    queue_name: str = "sda:ingest"
    dlq_key: str = "sda:ingest:dlq"
    dlq_max_len: int = Field(default=500, gt=0)
    state_ttl_s: int = Field(default=3600, gt=0)
    keep_result_s: int = Field(default=3600, gt=0)
    job_timeout_s: int = Field(default=900, gt=0)
    concurrency: int = Field(default=1, gt=0, validation_alias="INGEST_CONCURRENCY")
    progress_poll_s: float = Field(default=0.4, gt=0)
    progress_timeout_s: int = Field(default=1800, gt=0)


class AuthConfig(BaseSettings):
    """Local OIDC-shaped JWT auth. Self-contained on purpose: an external IdP would be the
    production choice, but the claims and the enforcement points are the same either way."""

    model_config = _SETTINGS_CONFIG

    jwt_secret: str = Field(min_length=1)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "smart-document-assistant"
    access_token_ttl_s: int = Field(default=43200, gt=0)
    # scrypt work factors: N=2**15 costs ~60ms per hash on a laptop core.
    scrypt_n: int = 2**15
    scrypt_r: int = 8
    scrypt_p: int = 1
    scrypt_salt_bytes: int = 16
    min_password_chars: int = Field(default=10, ge=8)
    roles: tuple[str, ...] = ("viewer", "editor", "admin")
    audit_page_size: int = Field(default=100, gt=0)
    # The admin role is reserved: only these addresses can hold it, whoever signs up or is
    # invited. Everyone else tops out at `signup_role`.
    admin_emails: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("nishanth.97872@gmail.com",), validation_alias="ADMIN_EMAILS"
    )
    signup_role: str = "editor"

    @field_validator("admin_emails", mode="before")
    @classmethod
    def _split_admin_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip().lower() for item in value.split(",") if item.strip())
        return value

    def is_admin_email(self, email: str) -> bool:
        return email.strip().lower() in self.admin_emails


# USD per million tokens, (prompt, completion). A model that is not listed costs 0 rather than
# a guess: a wrong number on a cost dashboard is worse than a visibly absent one. That is why
# the current Groq default is absent here -- set MODEL_PRICING_JSON with the rate on your plan.
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "llama-3.3-70b-versatile": (0.59, 0.79),
}


class ObservabilityConfig(BaseSettings):
    """Traces, metrics and logs. Every exporter is opt-in by env: with nothing set the process
    still records spans and metrics locally, it just ships them nowhere."""

    model_config = _SETTINGS_CONFIG

    service_name: str = Field(default="sda", validation_alias="OTEL_SERVICE_NAME")
    service_version: str = Field(default="1.0", validation_alias="SERVICE_VERSION")
    environment: str = Field(default="development", validation_alias="DEPLOY_ENV")
    # OTLP/HTTP base URL, e.g. http://otel-collector:4318 or http://jaeger:4318.
    otlp_endpoint: str = Field(default="", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    trace_sample_ratio: float = Field(
        default=1.0, ge=0.0, le=1.0, validation_alias="OTEL_TRACE_SAMPLE_RATIO"
    )
    db_spans: bool = Field(default=True, validation_alias="OTEL_DB_SPANS")
    # The worker has no HTTP server of its own, so it exposes /metrics on this port.
    worker_metrics_port: int = Field(
        default=9100, gt=0, lt=65536, validation_alias="WORKER_METRICS_PORT"
    )
    # Optional bearer token for /metrics. Unset means the endpoint is open, which is the norm
    # for a cluster-internal scrape target; it carries no tenant data either way.
    metrics_token: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    log_json: bool = True
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    pricing: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: dict(_DEFAULT_PRICING), validation_alias="MODEL_PRICING_JSON"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("pricing", mode="before")
    @classmethod
    def _parse_pricing(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else dict(_DEFAULT_PRICING)
        return value


class ModelConfig(BaseSettings):
    model_config = _SETTINGS_CONFIG

    embedder_name: str = "BAAI/bge-base-en-v1.5"
    embedder_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedder_max_tokens: int = Field(default=512, gt=0)
    reranker_name: str = "mixedbread-ai/mxbai-rerank-base-v1"
    blip_name: str = "Salesforce/blip-image-captioning-base"
    # Set either URL to move that forward pass out of the API process and onto a TEI service
    # (dynamic batching, ONNX/int8, its own scaling envelope). Unset means in-process weights.
    tei_embed_url: str = ""
    tei_rerank_url: str = ""
    tei_timeout_s: int = Field(default=60, gt=0)
    tei_max_connections: int = Field(default=16, gt=0)
    # Generation provider: "gemini" (default), "groq", "ollama" (offline fallback), or
    # "none" (retrieval only -- no key and no generation; used by the CI eval gate).
    llm_provider: Literal["gemini", "groq", "ollama", "none"] = "gemini"
    ollama_host: str = "127.0.0.1:11435"
    ollama_model: str = "qwen3:8b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    # Ordered failover chain, e.g. "groq,ollama". The primary above is always tried first and
    # is dropped from this list if repeated. Empty means no failover: one provider, as before.
    # NoDecode: the value is a comma-separated list, not the JSON pydantic-settings would
    # otherwise expect of a sequence field. The validator below does the splitting.
    llm_fallback_chain: Annotated[tuple[str, ...], NoDecode] = ()
    # Consecutive failures that open a provider's breaker, and how long it stays open before one
    # probe request is let through. Per process: a breaker shared in Redis would add a network
    # hop to the failure path, and each API worker learns the same outage within one request.
    llm_breaker_failures: int = Field(default=3, gt=0)
    llm_breaker_cooldown_s: float = Field(default=60.0, ge=0)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("llm_fallback_chain", mode="before")
    @classmethod
    def _parse_chain(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(name.strip().lower() for name in value.split(",") if name.strip())
        return value

    @field_validator("llm_fallback_chain", mode="after")
    @classmethod
    def _chain_names_are_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(value) - {"gemini", "groq", "ollama"}
        if unknown:
            raise ValueError(f"LLM_FALLBACK_CHAIN names unknown providers: {sorted(unknown)}")
        return value


class IngestionConfig(BaseSettings):
    model_config = _SETTINGS_CONFIG

    child_tokens: int = Field(default=200, gt=0)
    child_overlap_sentences: int = Field(default=1, ge=0)
    parent_tokens: int = Field(default=800, gt=0)
    max_upload_mb: int = Field(default=20, gt=0)
    # The body is streamed in these steps and spooled to RAM up to upload_spool_mb, then to a
    # temp file on disk: the cap is enforced as the bytes arrive, so an oversized POST is
    # refused instead of being made resident first.
    upload_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    upload_spool_mb: int = Field(default=4, gt=0)
    max_pages: int = Field(default=200, gt=0)
    min_chars_per_page: int = Field(default=40, ge=0)
    # A PDF whose text layer is below min_chars_per_page is scanned: it goes through OCR
    # instead of being rejected. OCR measures ~9s per page on this CPU, so the page cap is far
    # below max_pages -- a 50-page scan already spends most of the job's 15-minute timeout.
    ocr_enabled: bool = True
    ocr_max_pages: int = Field(default=50, gt=0)

    @model_validator(mode="after")
    def _child_fits_in_parent(self) -> "IngestionConfig":
        if self.child_tokens > self.parent_tokens:
            raise ValueError("child_tokens must not exceed parent_tokens")
        return self


class RetrievalConfig(BaseSettings):
    model_config = _SETTINGS_CONFIG

    mode: Literal["dense", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    modes: tuple[str, ...] = ("dense", "hybrid", "hybrid_rerank")
    dense_k: int = Field(default=30, gt=0)
    lexical_k: int = Field(default=30, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    rerank_candidates: int = Field(default=10, gt=0)
    context_k: int = Field(default=4, gt=0)
    context_token_budget: int = Field(default=3000, gt=0)
    max_per_doc: int = Field(default=2, gt=0)
    query_expansion: bool = True
    # Compound and preamble-laden questions dilute a single cross-encoder pair, so the question
    # is split into clauses and each passage keeps its best score. 1 disables the split.
    max_subqueries: int = Field(default=3, gt=0)
    min_subquery_words: int = Field(default=4, gt=0)
    expansion_variants: int = Field(default=3, gt=0)
    hyde: bool = False
    mmr_lambda: float = Field(default=0.7, ge=0.0, le=1.0)
    span_padding_tokens: int = Field(default=200, ge=0)
    dedup_jaccard: float = Field(default=0.9, ge=0.0, le=1.0)
    unused_passages_k: int = Field(default=6, ge=0)
    max_suggestions: int = Field(default=3, ge=0)
    # Fed from the selected threshold artifact, never from env. See config/thresholds/.
    calibration_logistic_a: float = 1.0
    calibration_logistic_b: float = 0.0


class SecurityConfig(BaseSettings):
    """Browser-facing and egress defences. Origins are per-deploy, so they come from env rather
    than a hardcoded localhost list that only ever works on a laptop."""

    model_config = _SETTINGS_CONFIG

    cors_allow_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("http://localhost:5173", "http://127.0.0.1:5173"),
        validation_alias="CORS_ALLOW_ORIGINS",
    )
    # The SPA is static files talking to the API over XHR and SSE, so it needs no inline script
    # and no eval. Set CSP_CONNECT_SRC when the API lives on a different origin from the SPA.
    csp_connect_src: str = Field(default="'self'", validation_alias="CSP_CONNECT_SRC")
    # 0 omits the header. Only meaningful behind TLS; a browser ignores it on plain http, and
    # sending it from a local dev server would pin localhost to https in the developer's browser.
    hsts_max_age_s: int = Field(default=0, ge=0, validation_alias="HSTS_MAX_AGE_S")
    # Wrap retrieved passages in delimiters and mark the span as data in the prompt. The
    # question side of this defence is unconditional; this is the document side.
    spotlight_documents: bool = Field(default=True, validation_alias="SPOTLIGHT_DOCUMENTS")
    # Check the generated answer for a leaked system prompt or an injected link before it is
    # returned. A hit degrades the answer to an abstention rather than editing it.
    scan_output: bool = Field(default=True, validation_alias="SCAN_OUTPUT")
    # Mask emails, phone numbers and government identifiers in logs, traces and prompt captures.
    redact_pii: bool = Field(default=True, validation_alias="REDACT_PII")
    # Refuse to start if a configured exporter or provider would send text off the box.
    no_egress: bool = Field(default=False, validation_alias="NO_EGRESS")
    # Deployment-wide operations -- reindex, cutover, rollback, the retention sweep -- span
    # every tenant, so a tenant admin's token is not enough authority for them. Unset means
    # those endpoints are disabled rather than open: an operator credential that defaults to
    # absent is the safe default, and a deployment that wants them sets one.
    operator_token: str = Field(default="", validation_alias="OPERATOR_TOKEN")

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("cors_allow_origins", mode="after")
    @classmethod
    def _origins_are_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError("CORS_ALLOW_ORIGINS cannot be '*': this API allows credentials")
        for origin in value:
            if not origin.startswith(("http://", "https://")) or origin.endswith("/"):
                raise ValueError(f"CORS_ALLOW_ORIGINS entry '{origin}' must be scheme://host[:port]")
        return value


class LimitsConfig(BaseSettings):
    """Per-tenant rate limits and spend budgets, enforced in Redis so every replica shares one
    counter. Rates are token buckets: a tenant may burst, then settles to the sustained rate."""

    model_config = _SETTINGS_CONFIG

    enabled: bool = Field(default=True, validation_alias="RATE_LIMIT_ENABLED")
    query_per_minute: float = Field(default=30, gt=0, validation_alias="QUERY_RATE_PER_MINUTE")
    query_burst: int = Field(default=10, ge=0, validation_alias="QUERY_BURST")
    ingest_per_minute: float = Field(default=6, gt=0, validation_alias="INGEST_RATE_PER_MINUTE")
    ingest_burst: int = Field(default=3, ge=0, validation_alias="INGEST_BURST")
    # Login and registration are limited by client address, not tenant: the caller has no token
    # yet, and this is the endpoint a credential-stuffing run hits.
    auth_per_minute: float = Field(default=10, gt=0, validation_alias="AUTH_RATE_PER_MINUTE")
    auth_burst: int = Field(default=5, ge=0, validation_alias="AUTH_BURST")
    # Daily budgets per tenant. 0 disables that budget. Spend is metered after each generation,
    # so the query that crosses a budget completes and the next one is refused.
    daily_tokens: int = Field(default=0, ge=0, validation_alias="DAILY_TOKEN_QUOTA")
    daily_cost_usd: float = Field(default=0.0, ge=0.0, validation_alias="DAILY_COST_USD")
    budget_ttl_s: int = Field(default=172800, gt=0)


class RetentionConfig(BaseSettings):
    """How long the system keeps what it is no longer serving.

    Every window here trades recoverability against storage and against a data-protection
    promise. They are days rather than a boolean because "deleted" and "unrecoverable" are
    different dates, and an operator has to be able to say what the gap is.
    """

    model_config = _SETTINGS_CONFIG

    # A deleted document stops being answerable immediately -- its chunks go with the delete --
    # and its row and bytes survive this long, so a delete made in error is recoverable.
    soft_delete_days: int = Field(default=30, ge=0, validation_alias="RETENTION_SOFT_DELETE_DAYS")
    # A document replaced by a newer upload of the same filename.
    superseded_days: int = Field(default=30, ge=0, validation_alias="RETENTION_SUPERSEDED_DAYS")
    # Chunks of an index build the alias no longer points at. This is the rollback window: once
    # they are purged, the cutover cannot be undone without another reindex.
    old_index_days: int = Field(default=7, ge=0, validation_alias="RETENTION_OLD_INDEX_DAYS")
    # 0 keeps the audit log forever, which is what a compliance regime usually wants.
    audit_days: int = Field(default=0, ge=0, validation_alias="RETENTION_AUDIT_DAYS")
    # How often the sweep runs in the worker. It is idempotent, so a missed run costs nothing.
    sweep_interval_s: int = Field(default=3600, gt=0, validation_alias="RETENTION_SWEEP_INTERVAL_S")
    enabled: bool = Field(default=True, validation_alias="RETENTION_ENABLED")


class GenerationConfig(BaseSettings):
    model_config = _SETTINGS_CONFIG

    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, gt=0)
    num_ctx: int = Field(default=8192, gt=0)
    timeout_s: int = Field(default=60, gt=0)
    keep_alive: str = "30m"
    history_turns: int = Field(default=6, ge=0)
    cache_ttl_s: int = Field(default=3600, ge=0)


class ApiConfig(BaseSettings):
    model_config = _SETTINGS_CONFIG

    # A query holds a worker thread for its whole life -- reranking and generation are both
    # blocking -- so the pool is what bounds concurrent forward passes. Past the cap the API
    # sheds load with a 503 instead of queueing behind an unbounded backlog.
    query_concurrency: int = Field(default=8, gt=0, validation_alias="QUERY_CONCURRENCY")
    query_timeout_s: int = Field(default=180, ge=0, validation_alias="QUERY_TIMEOUT_S")
    # Backpressure: the producer blocks once the client is this far behind.
    stream_queue_size: int = Field(default=64, gt=0)
    # How often a stalled stream re-checks the client, the deadline, and the clock.
    stream_poll_s: float = Field(default=0.5, gt=0)
    # A quiet stream only needs a frame often enough to stay under the proxy's read timeout;
    # reranking alone can be 20s of silence, and one comment per poll would be 40 of them.
    stream_keepalive_s: float = Field(default=10.0, ge=0)

    # Collection pages. The cap is what a caller gets for asking for more, not an error: a
    # client that wants everything should follow cursors, not widen the page.
    page_size: int = Field(default=50, gt=0, validation_alias="API_PAGE_SIZE")
    max_page_size: int = Field(default=200, gt=0, validation_alias="API_MAX_PAGE_SIZE")
    # How long an Idempotency-Key's record is replayable. A day: long enough for a client to
    # retry through an outage, short enough that keys are not a growing index.
    idempotency_ttl_s: int = Field(default=86400, gt=0, validation_alias="IDEMPOTENCY_TTL_S")
    # Files accepted in one bulk ingest call. Each still becomes its own job.
    max_bulk_files: int = Field(default=10, gt=0, validation_alias="MAX_BULK_FILES")


class WebhooksConfig(BaseSettings):
    """Outbound ingest-completion callbacks. Signed, bounded, and refused a private address by
    default: a webhook URL is caller-controlled input that this service then fetches."""

    model_config = _SETTINGS_CONFIG

    enabled: bool = Field(default=True, validation_alias="WEBHOOKS_ENABLED")
    max_per_tenant: int = Field(default=5, gt=0, validation_alias="WEBHOOKS_MAX_PER_TENANT")
    timeout_s: float = Field(default=5.0, gt=0, validation_alias="WEBHOOK_TIMEOUT_S")
    max_attempts: int = Field(default=3, gt=0, validation_alias="WEBHOOK_MAX_ATTEMPTS")
    backoff_s: float = Field(default=1.0, ge=0, validation_alias="WEBHOOK_BACKOFF_S")
    max_failures: int = Field(default=20, gt=0, validation_alias="WEBHOOK_MAX_FAILURES")
    # An SSRF control, not a convenience: without it a tenant can point a webhook at the
    # cluster's metadata service and have this service fetch it with its own network identity.
    # Set only for local development, where the receiver is on loopback.
    allow_private_targets: bool = Field(
        default=False, validation_alias="WEBHOOK_ALLOW_PRIVATE_TARGETS"
    )
    events: tuple[str, ...] = ("ingest.completed", "ingest.failed")


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: Paths
    api: ApiConfig
    storage: StorageConfig
    jobs: JobsConfig
    auth: AuthConfig
    observability: ObservabilityConfig
    models: ModelConfig
    ingestion: IngestionConfig
    retrieval: RetrievalConfig
    trust: TrustThresholds
    generation: GenerationConfig
    limits: LimitsConfig
    security: SecurityConfig
    webhooks: WebhooksConfig
    retention: RetentionConfig
    flags: Flags
    max_question_chars: int
    ingest_version: str
    retrieval_version: str


def _build_ingest_version(ingestion: IngestionConfig, embedder_name: str) -> str:
    payload = f"{ingestion.child_tokens}:{ingestion.child_overlap_sentences}:{ingestion.parent_tokens}:{embedder_name}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _build_retrieval_version(
    retrieval: RetrievalConfig, trust: TrustThresholds, models: ModelConfig
) -> str:
    """Identifies the configuration a cached answer was produced under.

    Part of the answer cache key: retuning a threshold or a retrieval knob must not keep
    serving answers computed under the old one, and an evaluation run must not read a previous
    run's answers back out of Redis.
    """
    payload = json.dumps(
        [retrieval.model_dump(), trust.model_dump(), models.embedder_name, models.reranker_name],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _egress_sinks(observability: ObservabilityConfig, models: ModelConfig) -> list[str]:
    """Configured sinks and providers that would put document or question text on the network."""
    configured = [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", observability.otlp_endpoint),
        ("LANGFUSE_PUBLIC_KEY", observability.langfuse_public_key),
        ("SENTRY_DSN", observability.sentry_dsn),
        ("TEI_EMBED_URL", models.tei_embed_url),
        ("TEI_RERANK_URL", models.tei_rerank_url),
    ]
    names = [name for name, value in configured if value]
    hosted = {models.llm_provider, *models.llm_fallback_chain} - {"ollama", "none"}
    if hosted:
        names.append(f"a hosted generation provider ({', '.join(sorted(hosted))})")
    return names


def build_settings(**overrides: Any) -> Settings:
    """Read and validate the environment. Called at import; call again after changing os.environ."""
    try:
        flags = Flags()
        artifact = thresholds.load(flags)
        ingestion = IngestionConfig()
        models = ModelConfig()
        observability = ObservabilityConfig()
        security = SecurityConfig()
        settings = Settings(
            paths=Paths(),
            api=ApiConfig(),
            storage=StorageConfig(),
            jobs=JobsConfig(),
            auth=AuthConfig(),
            observability=observability,
            models=models,
            ingestion=ingestion,
            retrieval=RetrievalConfig(**artifact.retrieval.model_dump()),
            trust=artifact.trust,
            generation=GenerationConfig(),
            limits=LimitsConfig(),
            security=security,
            webhooks=WebhooksConfig(),
            retention=RetentionConfig(),
            flags=flags,
            max_question_chars=2000,
            ingest_version=_build_ingest_version(ingestion, models.embedder_name),
            retrieval_version="",
        )
    except (ValidationError, SettingsError) as exc:
        raise ConfigError(f"Invalid configuration. See .env.example.\n{exc}") from exc

    if security.no_egress:
        sinks = _egress_sinks(observability, models)
        if sinks:
            raise ConfigError("NO_EGRESS=1 but these would send text off the box: " + ", ".join(sinks))

    return settings.model_copy(
        update={
            "retrieval_version": _build_retrieval_version(
                settings.retrieval, settings.trust, settings.models
            ),
            **overrides,
        }
    )


def replace(config: _ConfigT, **changes: Any) -> _ConfigT:
    """A validated copy with fields overridden -- dataclasses.replace for the settings models.

    Revalidates rather than copying: a test or a script that pokes a threshold gets the same
    bounds checks the environment gets, and cannot install a setting the process could not boot
    with. Never reads the environment, so the override is exactly what was asked for.
    """
    return type(config).model_validate({**config.model_dump(), **changes})


SETTINGS = build_settings()
