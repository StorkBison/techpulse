# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first for layer caching
COPY pyproject.toml README.md ./
COPY techpulse/__init__.py techpulse/__init__.py
RUN pip install ".[browser]" && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY techpulse ./techpulse
COPY tests/fixtures ./tests/fixtures
RUN pip install --no-deps . \
    && useradd --create-home --uid 1000 app && mkdir -p /app/data && chown -R app /app

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "techpulse.main:app", "--host", "0.0.0.0", "--port", "8000"]
