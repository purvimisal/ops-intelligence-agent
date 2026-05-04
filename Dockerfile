# Stage 1 — builder: install deps and bake in the BGE model
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the BGE model into the image so cold starts never hit the network
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('BAAI/bge-base-en-v1.5', cache_folder='/root/.cache/torch/sentence_transformers')"

# Stage 2 — runtime: lean image with only what's needed
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder (system-wide install → /usr/local)
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# Copy baked model cache
COPY --from=builder /root/.cache /root/.cache

# Non-root user
RUN useradd --create-home --shell /bin/bash opsagent
USER opsagent

COPY --chown=opsagent:opsagent app/ ./app/
COPY --chown=opsagent:opsagent runbooks/ ./runbooks/
COPY --chown=opsagent:opsagent scenarios/ ./scenarios/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
