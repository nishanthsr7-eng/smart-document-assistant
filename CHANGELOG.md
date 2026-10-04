# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-04

First tagged release. It covers everything from the first prototype to a multi-tenant service that is
ready for production.

### Core RAG pipeline
- PDF and TXT ingestion: docling for complex layouts, pypdf for simple PDFs, BLIP figure captions, OCR for scanned PDFs.
- Structure-aware parent/child chunking; table rows are never split.
- Hybrid retrieval: dense `bge-base-en-v1.5` vectors and lexical search fused with reciprocal rank fusion, then reranked by the `mxbai-rerank-base-v1` cross-encoder.
- Query routing, multi-query expansion, and splitting compound questions into parts.
- Pluggable generation (Gemini, Groq, Ollama), with failover between providers.

### Trust layer
- Abstention gate that refuses when the retrieved evidence is weak, with a retrieval-consensus override.
- Every sentence's citations are validated against the passage they cite.
- Confidence score built from four weighted signals; conflicting sources are detected and shown.
- Prompt-injection defense for both uploaded documents (nonce-fenced source blocks) and questions (instruction clauses are removed).

### Platform
- All shared state moved from embedded ChromaDB and local files to Postgres/pgvector, Redis and S3-compatible object storage.
- Ingestion moved off the request path into an arq worker; resumable uploads go straight to object storage.
- JWT auth with tenant isolation across the whole data path, per-tenant rate limits, and load shedding.
- RFC 9457 problem documents, idempotency keys, conditional requests, cursor pagination, and signed webhooks.
- Retention windows, tenant data erasure, PII redaction in logs and traces, and secret scanning of uploaded text.
- Reindexing swaps an alias, so it isn't visible to users; feature flags gate staged rollouts.

### Quality gates
- Evaluation over a 33-question golden set, gated in CI with thresholds versioned per environment.
- Backend tests run against real Postgres, Redis and object storage; property-based tests and a pinned SSE contract.
- Frontend rebuilt in React with Vitest coverage; `ruff` and `mypy` gate every pull request.

### Operations
- Container images (non-root, read-only filesystem, model weights baked in), a Helm chart, and signed images with supply-chain scanning.
- OpenTelemetry traces, Prometheus metrics, JSON logs, Langfuse LLM traces, and Grafana dashboards.
- SLOs, runbooks, on-call guide, backup and restore drill, and a k6 load profile.

[1.0.0]: https://github.com/nishanthsr7-eng/smart-document-assistant/releases/tag/v1.0.0
