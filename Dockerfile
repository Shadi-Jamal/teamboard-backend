# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
# Installs the application and its pinned dependencies into a virtualenv. Build
# tools and pip's cache stay in this stage and never reach the final image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml constraints.txt ./
COPY src ./src
# constraints.txt pins every (transitive) dependency to the versions CI tested.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt .

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged user with a fixed numeric ID (so `runAsNonRoot`-style checks work).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Migrations ship with the image: `alembic upgrade head` runs as a one-off task.
COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001
EXPOSE 8000

# Liveness only; readiness (database, Redis) is /readyz and belongs to the
# orchestrator / load balancer. The worker overrides or disables this check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"]

CMD ["python", "-m", "teamboard"]
