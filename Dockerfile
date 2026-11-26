FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv --no-cache-dir

# Copy dependency files first for layer caching
COPY pyproject.toml ./
COPY risk/__init__.py risk/

# Install dependencies (no dev extras in production)
RUN uv pip install --system --no-cache ".[dev]" 2>/dev/null || \
    uv pip install --system --no-cache .

# Copy source
COPY . .

# Install the package itself
RUN uv pip install --system --no-cache -e .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "risk.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
