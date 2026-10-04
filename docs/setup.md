# Setup

Manual host setup, tests and evaluation. For the one-command container path see the [README](../README.md#quick-start).

### Prerequisites
- Python 3.10+
- Docker (for Postgres, Redis and MinIO — the service has no local-disk fallback)
- A free API key from [Google AI Studio](https://aistudio.google.com/apikey) (default provider) — or [Ollama](https://ollama.com) installed if you'd rather run fully offline

### Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 2. Install CPU-only PyTorch (avoids pulling a ~2GB CUDA build). torchvision from the same
#    index: docling pulls it in, and PyPI's build fails to register its ops against a CPU torch.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the environment file and add your key
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/Mac
# edit .env and set GEMINI_API_KEY (or switch LLM_PROVIDER=ollama, see Provider Choice in the README)
# the DATABASE_URL / REDIS_URL / S3_* defaults already match docker-compose.yml
# set JWT_SECRET -- the API will not start without it:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# 5. Start the shared state backends and apply the schema
docker compose up -d
alembic upgrade head

# 6. Run the FastAPI backend (now safe to run with multiple workers)
uvicorn src.api.router:app --host 127.0.0.1 --port 8000 --workers 4

# 7. In a second terminal, start the ingest worker (uploads are queued, not parsed in the API)
python -m arq src.ingestion.worker.WorkerSettings

# 8. In a third terminal, start the React frontend
cd frontend
npm install
npm run dev
```

If using `LLM_PROVIDER=ollama` instead, pull the model and start a project-scoped server before step 5:

```bash
set OLLAMA_HOST=127.0.0.1:11435
set OLLAMA_MODELS=data\models\ollama
ollama pull qwen3:8b
ollama serve
# (keep this terminal open)
```

The app opens at `http://localhost:5173`. Create a workspace on the sign-in screen (the first user
of a workspace is its admin), then upload a PDF or TXT file, wait for indexing, and ask a question.

### Running in containers

To run the whole stack in containers instead of on the host:

```bash
docker compose --profile app up -d --build
```

That builds two images from `docker/`, applies migrations as a one-shot `migrate` service, then
starts the API, the ingest worker and an nginx-served SPA on `http://localhost:8080`. The API and
the worker are the *same* image with a different entrypoint argument (`api` / `worker` /
`migrate`), so they cannot drift apart on dependencies.

The backend image bakes the model weights in: bge, mxbai, BLIP and the docling artifacts are
~1.5 GB, and pulling them at boot would mean minutes of downloading before a fresh replica is
ready. `HF_HUB_OFFLINE=1` in the image makes a missed bake fail loudly instead of silently
reaching for HuggingFace at runtime. The first build is therefore slow; layers cache afterwards.

Both images run as a non-root user with a read-only root filesystem.

For Kubernetes, TEI inference, observability, CI and the full list of environment variables,
see [docs/operations.md](operations.md).

### Running Tests

```bash
pip install -r requirements-dev.txt
pytest -q --cov=src --cov-fail-under=75     # backend; needs the compose stack up
cd frontend && npm test                     # Vitest
```

The backend suite runs against real Postgres, Redis and object storage rather than mocks, because
the bugs worth catching here are the ones that only appear when state is shared. It resets every
tenant, so do not run it against a stack that is serving anything you care about.

### Running Evaluation

```bash
python -m evaluation.run_eval --mode hybrid --generate
```

The run ingests `data/sample_docs` into a reserved evaluation tenant. The test suite resets every
tenant, so do not run `pytest` against the same stack while an evaluation is in flight.
