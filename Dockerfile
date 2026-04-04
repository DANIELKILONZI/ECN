# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# ECN — Executable Consensus Network
# Multi-stage Docker image for the ECN API + supply-chain node network.
#
# Build:
#   docker build -t ecn:latest .
#
# Run (single-container, all nodes in-process):
#   docker run -p 8000:8000 ecn:latest
#
# Run a standalone node server (for multi-container deployments):
#   docker run -p 9001:9001 ecn:latest \
#       python -m ecn.node_server --node-id Node-1 --port 9001 --host 0.0.0.0
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 1 — dependency layer (cached separately from source code)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

WORKDIR /app

# Install OS-level build dependencies needed by cryptography
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy only the installed packages from the deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source
COPY ecn/ ./ecn/

# Non-root user for security
RUN useradd -m -u 1000 ecn && chown -R ecn:ecn /app
USER ecn

# Port exposed by the ECN REST API
EXPOSE 8000

# Health check — hits the network/state endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/network/state')" || exit 1

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Default: start the ECN REST API (all nodes in-process)
CMD ["uvicorn", "ecn.api:app", "--host", "0.0.0.0", "--port", "8000"]
