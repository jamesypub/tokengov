# Multi-stage build for the self-contained tg-container image
# (#479). Final image runs api+worker with the React UI baked in
# and NO build toolchain (no node/npm, no compiler) at runtime.
#
# Build context is the REPO ROOT (so both container/ and
# admin-ui/web/ are reachable):
#   docker build -f container/Dockerfile -t tg-container:latest .
#
# Account/region generic — nothing AWS-specific is baked in; the
# image reads config from env at runtime.

# ── Stage 1: build the React SPA ─────────────────────────────
# Pinned node tag; only used at build time, never shipped.
FROM node:22-slim AS ui
WORKDIR /ui
# Install deps against the lockfile first for layer caching.
COPY admin-ui/web/package.json admin-ui/web/package-lock.json ./
RUN npm ci
# Then the source + build → /ui/dist (vite default outDir).
COPY admin-ui/web/ ./
RUN npm run build

# ── Stage 2: python runtime (api + worker) ───────────────────
# psycopg2-binary ships a manylinux wheel bundling libpq, so no
# gcc/libpq-dev is needed — keeping the runtime free of a
# compiler reduces post-RCE blast radius.
FROM python:3.12-slim AS runtime
WORKDIR /app

# #538: bake the product version into the image so /api/version
# (api/main.py reads $TG_VERSION) returns a real value instead of
# the "dev" fallback that rendered as "vdev" in the UI footer. The
# installer passes --build-arg TG_VERSION=$(git describe ...);
# docker-compose passes it via build.args. Defaults to 'dev' for a
# bare `docker build` with no arg.
ARG TG_VERSION=dev
ENV TG_VERSION=${TG_VERSION}
# Lockstep image<->CFN-template versioning: expose the baked version as
# a manifest LABEL too (not only the runtime ENV /api/version reads), so
# the installer can read the image's version from the registry manifest
# BEFORE creating the stack and refuse a CFN template that needs a newer
# image (the deploy-skew the {{DATE_FILTER}} demo0 break exposed) —
# without pulling/running the image. Same value as the ENV; provenance
# hygiene regardless.
LABEL org.tg.version="${TG_VERSION}"

COPY container/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source (api/, worker/, db/, etc.).
COPY container/ .

# Bake the built SPA into /app/static — the path api/main.py
# serves from. This replaces the old "tg-local-install.sh copies
# dist into container/static/" step for the image build, so the
# image is self-contained from source.
COPY --from=ui /ui/dist/ ./static/

# Drop privileges. uvicorn doesn't need root, and the api/worker
# never touch privileged ports (api binds :8000).
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

# api:    uvicorn api.main:app
# worker: python -m worker.main  (override CMD)
ENTRYPOINT []
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
