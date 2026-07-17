# ============================================================
# Trading Backend — Docker Build
# ============================================================
# Runs the autonomous trading cycle (collect → analyze → trade)
# as a standalone container with its own copy of the backend code.
#
# Build:
#   cd sun/trading-service
#   docker build -t trading-service .
# ============================================================

FROM python:3.11-slim AS deps

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Production runner ─────────────────────────────────────────
FROM python:3.11-slim AS runner
WORKDIR /app

# Install wget for healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --system --gid 1001 appgrp \
    && useradd --system --uid 1001 --gid appgrp -m -d /home/appusr appusr

# Create logs directory
RUN mkdir -p /app/logs && chown -R appusr:appgrp /app/logs

# ── Copy Python venv ──────────────────────────────────────────
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Playwright / Chromium (absorbed scraper-service engines) ─────────────────
# The folded-in scraper (app.scraper: playwright/crawl4ai/vision engines) drives
# headless Chromium. Install the browser's OS dependencies as root, then bake the
# browser binaries into appusr's cache below (after USER appusr). Chromium binary
# comes from `playwright install`, NOT pip.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# ── Copy backend source ──────────────────────────────────────
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY tool_schemas.json ./tool_schemas.json

# ── Copy the cycle backend entrypoint ────────────────────────
COPY cycle_main.py ./cycle_main.py
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

RUN mkdir -p /app/logs/cycles /app/logs/v2 /app/memory

RUN chown -R appusr:appgrp /app

ENV PYTHONPATH="/app"
ENV SHARED_CODEBASE_PATH="/app"

USER appusr

# Bake Playwright Chromium into appusr's cache (~/.cache/ms-playwright) so the
# absorbed scraper engines have a browser at runtime as the non-root user.
RUN playwright install chromium

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD wget --no-verbose --tries=1 --output-document=/dev/null http://localhost:8080/health || exit 1

CMD ["./entrypoint.sh"]
