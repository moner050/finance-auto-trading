FROM python:3.14.7-slim

ARG GATE_COMMIT_SHA
ARG GATE_MIGRATION_HEAD
ARG GATE_OFFICIAL_FACT_REGISTRY_SHA256
LABEL org.opencontainers.image.revision="${GATE_COMMIT_SHA}" \
      io.autotrader.migration-head="${GATE_MIGRATION_HEAD}" \
      io.autotrader.official-fact-registry-sha256="${GATE_OFFICIAL_FACT_REGISTRY_SHA256}"

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv==0.12.3 \
    && uv sync --frozen --no-cache --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-cache --no-dev \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.14/ensurepip \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.14
