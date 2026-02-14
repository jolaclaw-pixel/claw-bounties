# Stage 1: Install dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy only runtime files (no tests, no dev deps)
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY alembic/ alembic/
COPY alembic.ini .
COPY SKILL.md .

# Create data directory
RUN mkdir -p /data

# Cache buster: 2026-02-14T06:30

# Environment
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
