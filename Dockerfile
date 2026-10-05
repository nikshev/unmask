# Production Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY tests/fixtures/real/ ./tests/fixtures/real/

# Install package in development mode so imports work
RUN uv pip install -e .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["uv", "run", "python", "scripts/serve_unmask.py", "--port", "8080"]