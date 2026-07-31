# syntax=docker/dockerfile:1.10
#
# One Dockerfile, three images. Pick the deployable at build time:
#
#   podman build --build-arg SERVICE=cb-gateway -t cb-gateway:dev .
#   podman build --build-arg SERVICE=cb-api     -t cb-api:dev     .
#   podman build --build-arg SERVICE=cb-worker  -t cb-worker:dev  .
#
# Base: Wolfi (cgr.dev/chainguard/wolfi-base). glibc, apk, and small — which is
# what this stack needs. Alpine would be smaller still and musl would force
# source builds of asyncpg, polars, obstore and blake3 (their wheels are
# manylinux/glibc); distroless has no package manager, which makes ffmpeg and
# libstdc++ someone else's problem to vendor by hand.
#
# Size, measured by what actually costs bytes here:
#   * the workspace is installed per-service (`uv sync --package`), so cb-worker
#     does not carry FastAPI and nothing carries cb-sandbox's DuckDB (~50 MB)
#   * bytecode is compiled at build time and the sources dropped (STRIP_SOURCE)
#   * .so files are stripped of debug symbols
#   * uv, gcc and the headers stay in the builder stage
#
# COMPILE=cython (default) keeps the repo's existing policy: cb-core's hot
# modules are Cython-compiled by its own setup.py, everything else ships as
# optimised bytecode. COMPILE=nuitka additionally compiles the pure-Python
# packages into a single binary — read the caveats on that stage before using it.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.5.14
ARG SERVICE=cb-gateway
ARG COMPILE=cython
ARG STRIP_SOURCE=true
# 2 also strips docstrings. FastAPI only uses them for OpenAPI descriptions, and
# nothing in this tree uses `assert` for control flow — but if a dependency does,
# drop this to 1.
ARG PYTHONOPTIMIZE=2


# --------------------------------------------------------------------------- #
# builder — uv, compilers, headers. None of this reaches the runtime image.
# --------------------------------------------------------------------------- #
FROM cgr.dev/chainguard/wolfi-base:latest AS builder

ARG PYTHON_VERSION
ARG UV_VERSION
ARG SERVICE
ARG PYTHONOPTIMIZE

RUN apk add --no-cache \
      "python-${PYTHON_VERSION}" \
      "python-${PYTHON_VERSION}-dev" \
      build-base \
      binutils \
      git

COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

ENV UV_PYTHON=/usr/bin/python${PYTHON_VERSION} \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/cb \
    PYTHONOPTIMIZE=${PYTHONOPTIMIZE} \
    PYTHONDONTWRITEBYTECODE=0

WORKDIR /src

# Dependency layer: only the manifests, so a code change does not re-resolve or
# re-download the wheel set. uv needs every workspace member's pyproject even
# when installing one of them.
COPY pyproject.toml uv.lock ./
COPY packages/cb-core/pyproject.toml     packages/cb-core/
COPY packages/cb-api/pyproject.toml      packages/cb-api/
COPY packages/cb-gateway/pyproject.toml  packages/cb-gateway/
COPY packages/cb-worker/pyproject.toml   packages/cb-worker/
COPY packages/cb-sandbox/pyproject.toml  packages/cb-sandbox/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --no-editable --package "${SERVICE}"

# Source layer. cb-core's setup.py runs cythonize() here — the .so lands in the
# venv, and the module stays importable as pure Python if the build is skipped.
COPY packages/ packages/
COPY deploy/docker/launcher.py /src/cb_launcher.py

# --no-editable is load-bearing: uv installs workspace members as editable by
# default, which writes a .pth pointing at /src. That path does not exist in the
# runtime stage, so an editable install produces an image where `import cb_core`
# fails — and only fails there, because the builder still has /src.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${SERVICE}"

# The launcher is imported as a top-level module, not part of a package.
RUN cp /src/cb_launcher.py "/opt/cb/lib/python${PYTHON_VERSION}/site-packages/cb_launcher.py" \
 && /opt/cb/bin/python -c "import cb_launcher; print('launcher ok')"

# Migrations are data, not code: alembic reads them from disk at runtime.
COPY packages/cb-api/migrations/ /opt/cb/share/cb-migrations/
COPY packages/cb-api/alembic.ini /opt/cb/share/cb-migrations/alembic.ini


# --------------------------------------------------------------------------- #
# nuitka — optional. Compiles the pure-Python packages into one binary.
#
# Honest expectations before you turn this on:
#   * It does NOT shrink the big things. pydantic-core, asyncpg, polars,
#     obstore, blake3, uvloop and granian are already-compiled extension
#     modules; Nuitka copies them verbatim. The saving is the interpreter's
#     stdlib source plus our own .py — tens of MB, not hundreds.
#   * It breaks anything resolved dynamically unless named explicitly:
#     aiogram's handler discovery, alembic's migration modules, and
#     opentelemetry's entry-point-based instrumentation all qualify.
#   * Build time goes from ~1 min to ~10.
# Worth it for a fixed edge deployment; not obviously worth it for a cluster
# that pulls layers once and caches them.
# --------------------------------------------------------------------------- #
FROM builder AS nuitka

ARG PYTHON_VERSION
ARG SERVICE
ARG PYTHONOPTIMIZE

RUN apk add --no-cache patchelf ccache
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/cb/bin/python nuitka

RUN --mount=type=cache,target=/root/.cache/ccache \
    cd /src && /opt/cb/bin/python -m nuitka \
      --standalone \
      --output-dir=/nuitka \
      --output-filename=cookiebot \
      --python-flag=-OO \
      --assume-yes-for-downloads \
      --no-deployment-flag=self-execution \
      --include-package=cb_core \
      --include-package=cb_api \
      --include-package=cb_gateway \
      --include-package=cb_worker \
      --include-package=aiogram \
      --include-package=arq \
      --include-package=alembic \
      --include-package=opentelemetry \
      --include-package-data=cb_core \
      --nofollow-import-to=pytest \
      --nofollow-import-to=cb_sandbox \
      cb_launcher.py

RUN find /nuitka/cb_launcher.dist -name "*.so" -exec strip --strip-unneeded {} + || true


# --------------------------------------------------------------------------- #
# prune — shared cleanup, applied to whichever build produced the venv
# --------------------------------------------------------------------------- #
FROM builder AS prune

ARG PYTHON_VERSION
ARG STRIP_SOURCE

RUN set -eux; \
    SP="/opt/cb/lib/python${PYTHON_VERSION}/site-packages"; \
    # debug symbols in wheels are pure weight in a container
    find "$SP" -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true; \
    # tests, headers and type stubs that no runtime imports
    find "$SP" -type d \( -name tests -o -name test -o -name "__pycache__.dist-info" \) -prune -exec rm -rf {} + 2>/dev/null || true; \
    find "$SP" -type f \( -name "*.pyi" -o -name "*.c" -o -name "*.h" -o -name "*.pyx" -o -name "*.html" \) -delete 2>/dev/null || true; \
    rm -rf "$SP"/pip "$SP"/setuptools "$SP"/pkg_resources 2>/dev/null || true; \
    # Cython stays, despite looking like a build-only dependency: cb_core's
    # pure-Python-mode modules (dedupe.py, textmatch.py) `import cython` at
    # runtime, and the shim lives inside the Cython package. Deleting it costs
    # 11 MB and breaks every import of cb_core.storage.
    #   rm -rf "$SP"/Cython   <- do not
    if [ "$STRIP_SOURCE" = "true" ]; then \
      # Sourceless imports need the .pyc where the .py was — Python does NOT
      # load `__pycache__/mod.cpython-313.pyc` once `mod.py` is gone. `-b` is
      # what writes the legacy layout; deleting sources without it produces an
      # image whose every import fails, which is exactly what happened the first
      # time this was built.
      "/opt/cb/bin/python" -m compileall -q -o 2 -b \
        --invalidation-mode unchecked-hash "$SP" >/dev/null 2>&1 || true; \
      find "$SP" -name "*.py" -delete; \
      find "$SP" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true; \
    fi; \
    find /opt/cb -name "*.dist-info" -type d -exec rm -rf {}/RECORD \; 2>/dev/null || true; \
    # A sourceless build must still import. Fail here rather than in the cluster.
    "/opt/cb/bin/python" -c "import cb_launcher, cb_core; print('prune: imports ok')"


# --------------------------------------------------------------------------- #
# runtime — interpreter, libstdc++, ffmpeg for cb-worker. Nothing else.
# --------------------------------------------------------------------------- #
FROM cgr.dev/chainguard/wolfi-base:latest AS runtime-cython

ARG PYTHON_VERSION
ARG SERVICE
ARG PYTHONOPTIMIZE

# ffmpeg only where it is used: cb-worker's media path shells out to it, and it
# is ~80 MB that cb-api and cb-gateway have no reason to carry.
RUN apk add --no-cache \
      "python-${PYTHON_VERSION}" \
      libstdc++ \
      ca-certificates-bundle \
      tzdata \
 && if [ "${SERVICE}" = "cb-worker" ]; then apk add --no-cache ffmpeg; fi \
 && rm -rf /var/cache/apk/*

COPY --from=prune /opt/cb /opt/cb

ENV PATH="/opt/cb/bin:${PATH}" \
    PYTHONOPTIMIZE=${PYTHONOPTIMIZE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CB_SERVICE=${SERVICE} \
    CB_MIGRATIONS_DIR=/opt/cb/share/cb-migrations

# wolfi-base already defines nonroot at 65532 — creating it again fails the build.
# Import the world once, in the image that ships. The builder cannot catch a
# missing module: it still has /src and the build tooling on disk.
RUN /opt/cb/bin/python -c "import cb_launcher, cb_core; print('runtime imports ok')"

USER 65532:65532

ENTRYPOINT ["/opt/cb/bin/python", "-m", "cb_launcher"]


FROM cgr.dev/chainguard/wolfi-base:latest AS runtime-nuitka

ARG SERVICE

RUN apk add --no-cache libstdc++ ca-certificates-bundle tzdata \
 && if [ "${SERVICE}" = "cb-worker" ]; then apk add --no-cache ffmpeg; fi \
 && rm -rf /var/cache/apk/*

COPY --from=nuitka /nuitka/cb_launcher.dist /opt/cb
COPY --from=builder /opt/cb/share/cb-migrations /opt/cb/share/cb-migrations

ENV PYTHONUNBUFFERED=1 \
    CB_SERVICE=${SERVICE} \
    CB_MIGRATIONS_DIR=/opt/cb/share/cb-migrations

# wolfi-base already defines nonroot at 65532 — creating it again fails the build.
USER 65532:65532

ENTRYPOINT ["/opt/cb/cookiebot"]


# --------------------------------------------------------------------------- #
# final — selected by COMPILE
# --------------------------------------------------------------------------- #
FROM runtime-cython AS final-cython
FROM runtime-nuitka AS final-nuitka

FROM final-${COMPILE} AS final

ARG SERVICE
ARG COMPILE
ARG GIT_SHA=unknown
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="cookiebot-${SERVICE}" \
      org.opencontainers.image.description="Cookiebot v2 — ${SERVICE}" \
      org.opencontainers.image.source="https://github.com/Cookiebot-Team/cookiebot-telegram-bot" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      com.cookiebot.compile="${COMPILE}"
