FROM python:3.12-slim

# Prevent python from writing pyc and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    "fastapi>=0.115,<1" \
    "uvicorn>=0.34,<1" \
    "Scrapy>=2.18,<3" \
    "httpx>=0.28" \
    "redis>=5.2" \
    "asyncpg>=0.30" \
    "pgvector>=0.3" \
    "trafilatura>=2.0" \
    "beautifulsoup4>=4.13" \
    "pydantic>=2.10" \
    "numpy>=2.0" \
    "fastembed>=0.4" \
    "flashrank>=0.2"

# Copy application code
COPY services/ ./services/
COPY crawler/ ./crawler/
COPY run_all.py .
COPY docker-compose.yml .

# Expose API port
EXPOSE 8000

# Default entrypoint runs all services in container
CMD ["python", "run_all.py", "--no-docker"]
