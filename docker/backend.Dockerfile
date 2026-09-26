# syntax=docker/dockerfile:1.7
# One image, three roles: the API, the ingest worker and the migration job differ only in the
# entrypoint argument, so they cannot drift apart on dependencies.
ARG PYTHON_VERSION=3.10
ARG TORCH_VERSION=2.14.0
ARG TORCHVISION_VERSION=0.29.0

# opencv, pulled in by docling, links against the X client libraries even when run headless.
FROM python:${PYTHON_VERSION}-slim AS deps
ARG TORCH_VERSION
ARG TORCHVISION_VERSION
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PATH=/opt/venv/bin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libgomp1 libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 \
 && rm -rf /var/lib/apt/lists/*
# The base image's pip 23.0.1 mis-normalizes package names on the torch CPU index.
RUN python -m venv /opt/venv && pip install --upgrade pip
COPY requirements.txt ./
# torchvision from the same index: docling pulls it transitively, and the PyPI build does not
# register its ops against a CPU torch ("operator torchvision::nms does not exist" at import).
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
      "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
 && pip install -r requirements.txt


# Weights are baked rather than pulled at boot: ~1.5 GB across bge, mxbai, BLIP and docling
# would otherwise be minutes of downloading before a fresh pod is ready.
FROM deps AS models
WORKDIR /app
COPY src ./src
COPY docker/bake_models.py ./
RUN DATABASE_URL=bake REDIS_URL=bake S3_ENDPOINT=bake S3_BUCKET=bake \
    S3_ACCESS_KEY=bake S3_SECRET_KEY=bake JWT_SECRET=bake \
    python bake_models.py /opt/models/hf/hub /opt/models/artifacts


FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/data/models/hf \
    HF_HUB_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgomp1 libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 sda
COPY --from=deps /opt/venv /opt/venv
COPY --from=models --chown=10001:10001 /opt/models/hf /app/data/models/hf
COPY --from=models --chown=10001:10001 /opt/models/artifacts /app/data/models
WORKDIR /app
COPY --chown=10001:10001 alembic.ini ./
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
USER 10001
EXPOSE 8000
ENTRYPOINT ["entrypoint.sh"]
CMD ["api"]
