FROM python:3.11-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

RUN useradd --create-home --uid 10001 patchflow

WORKDIR /app
COPY pyproject.toml README.md USAGE.md ./
COPY agent ./agent
COPY config ./config
COPY context ./context
COPY entry ./entry
COPY integrations ./integrations
COPY llm ./llm
COPY service ./service
COPY tools ./tools
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install ".[dev]"

RUN mkdir -p /data/logs /workspace/repos && chown -R patchflow:patchflow /app /data /workspace
USER patchflow

CMD ["uvicorn", "service.api:app", "--host", "0.0.0.0", "--port", "8000"]
