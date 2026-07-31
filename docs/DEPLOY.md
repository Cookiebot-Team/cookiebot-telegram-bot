# Deploying Cookiebot v2

Three images, one Helm chart. The chart lives here because its shape versions
with the code; the *values* live in the infrastructure repo, because that is
where a cluster decides what runs.

## Images

One `Dockerfile`, three outputs, selected by `--build-arg SERVICE`. Podman locally,
Docker in CI — the Dockerfile uses nothing BuildKit-only, so both work:

```sh
podman build --build-arg SERVICE=cb-gateway -t cb-gateway:dev .
podman build --build-arg SERVICE=cb-api     -t cb-api:dev     .
podman build --build-arg SERVICE=cb-worker  -t cb-worker:dev  .
```

Quote the tag: in zsh, `-t $s:test` silently applies the `:t` history modifier and
you get `cb-gatewayest`. Use `-t "${s}:test"`.

### Measured sizes (linux/arm64, `COMPILE=cython`)

| Image | Size | Why it differs |
|---|---|---|
| `cb-api` | 338 MB | |
| `cb-gateway` | 344 MB | aiogram |
| `cb-worker` | 493 MB | ffmpeg (~150 MB installed) |

Verified absent from `cb-api`: `duckdb`, `cb_sandbox`. Verified absent from
`cb-gateway`: `ffmpeg`. The per-service split does what it claims.

**The biggest remaining item is not ours to strip.** Inside the image:

```
132.7M  site-packages/_polars_runtime_32     <- 40% of the image
 17.9M  granian
 16.1M  grpc                                 (otel OTLP exporter)
 15.0M  psycopg_binary.libs                  (alembic DDL only; asyncpg does runtime SQL)
 11.5M  Cython                               (runtime dependency — see below)
 57.9M  /usr/lib                             (interpreter + libstdc++)
```

`polars` is a `cb-core` dependency used for analytics rollups, so every service
carries 133 MB of it. Moving it to an extra that only `cb-worker` and `cb-api`
install would cut `cb-gateway` — the one that scales by replica count — roughly
in half. That is a `pyproject.toml` change, not a packaging change.

CI publishes them to GHCR on every push to `main`
(`.github/workflows/docker.yml`): `dev-<sha>` is what GitOps pins, `dev` is a
moving tag for humans poking at the cluster. Pull requests build and smoke-test
but never push — a PR must not be able to publish an image the cluster pulls.

### Base image: Wolfi

`cgr.dev/chainguard/wolfi-base`. The ask was "small, glibc, with a package
manager", and this is the one that is all three:

| | glibc | package manager | notes |
|---|---|---|---|
| **Wolfi** | ✅ | apk | ~12 MB base, current packages, this is the pick |
| Alpine | ❌ musl | apk | smaller, but asyncpg / polars / obstore / blake3 publish manylinux wheels — musl means building them from source |
| debian-slim | ✅ | apt | ~75 MB base, older Python |
| distroless | ✅ | ❌ | no way to add ffmpeg or libstdc++ without vendoring by hand |

### What actually makes the image small

Not one trick — five, in descending order of how many bytes they save:

1. **Per-service installs.** `uv sync --package cb-worker` installs that member
   and its dependencies only. `cb-worker` never carries FastAPI, and nothing
   carries `cb-sandbox`'s DuckDB (~50 MB on its own).
2. **ffmpeg only in `cb-worker`.** ~80 MB the gateway and API have no use for.
3. **Builder/runtime split.** uv, gcc, headers and the uv cache stay in the
   builder stage.
4. **Stripped `.so` files.** Debug symbols in wheels are pure weight in a
   container.
5. **Bytecode without sources.** `UV_COMPILE_BYTECODE=1` plus
   `PYTHONOPTIMIZE=2` at build time, then the `.py` files are deleted
   (`--build-arg STRIP_SOURCE=false` to keep them when you need readable
   tracebacks inside the container).

### "Compile the Python to a binary"

Two things are worth separating, because only one of them is free.

**Optimised code — on by default.** This repo already compiles its hot path with
Cython under a benchmark gate: a module ships compiled only if it reaches 1.5×,
and `packages/cb-core/setup.py` documents the three modules that were *removed*
from that list for failing it. The image build runs the same `setup.py`, so the
`.so` lands in the venv exactly as it does locally. On top of that, everything
else ships as `-OO` bytecode.

**A single binary — `--build-arg COMPILE=nuitka`, off by default.** Implemented,
and here is the honest accounting before you turn it on:

- It does **not** shrink the big things. `pydantic-core`, `asyncpg`, `polars`,
  `obstore`, `blake3`, `uvloop` and `granian` are already-compiled extension
  modules; Nuitka copies them verbatim. What it removes is the interpreter's
  stdlib source and our own pure-Python — tens of MB, not hundreds.
- It breaks anything resolved dynamically unless named explicitly. aiogram's
  handler discovery, alembic's migration modules and OpenTelemetry's
  entry-point instrumentation all qualify; the `--include-package` list in the
  Dockerfile covers the ones we know about, and a new dependency can add
  another.
- Build time goes from about a minute to about ten.

Worth it for a fixed edge deployment. Not obviously worth it for a cluster that
pulls a layer once and caches it — which is why `cython` is the default.

Either way the entrypoint is `deploy/docker/launcher.py`: one module with static
imports that starts granian or arq in-process. `scripts/cb.py` shells out to
`uv run granian ...`, which is right for a laptop and wrong for a container —
it would need uv, the lockfile and the workspace at runtime.

```
CB_SERVICE=cb-gateway  → granian, ASGI, cb_gateway.main:app on :8081
CB_SERVICE=cb-api      → granian, ASGI, cb_api.main:app     on :8000
CB_SERVICE=cb-worker   → arq, WorkerSettings
CB_SERVICE=migrate     → ensure_schema(), then exit
```

## Chart

```sh
helm install cookiebot deploy/helm/cookiebot -n cookiebot-uat --create-namespace
```

Deploys the three services plus, on by default and each independently
switchable: a self-hosted Telegram Bot API server, Valkey, and a
CloudNativePG/Citus cluster. It does **not** install the CNPG operator — that is
a cluster concern.

| Value | Default | Turn it off when |
|---|---|---|
| `telegramBotApi.enabled` | `true` | you want api.telegram.org and a public webhook |
| `valkey.enabled` | `false`-able | a managed Redis exists → set `valkey.externalDsn` |
| `citus.enabled` | `true` | managed Postgres exists → `citus.externalDsn` / `externalDsnSecret` |
| `migrations.enabled` | `true` | never, really — it is what stops N replicas racing the same DDL |

### The webhook path is entirely private

With `telegramBotApi.enabled=true` and `local: true`, the chart sets
`CB_WEBHOOK_BASE_URL` to a ClusterIP address. The gateway registers that with
`setWebhook` at startup (`cb_gateway/ingest.py`), and our own Bot API server
POSTs updates to it from inside the cluster:

```
Telegram DCs ──MTProto──► telegram-bot-api (--local) ──HTTP──► cb-gateway
```

No public hostname, no ingress, no tunnel entry — and no 20 MB download cap,
which is what the v1 media path kept tripping over. `--local` is what makes an
`http://` webhook on a private address legal.

Two prerequisites the chart cannot enforce:

1. `telegram-api-id` / `telegram-api-hash` in the Secret, from
   [my.telegram.org](https://my.telegram.org). These are *user* credentials, not
   bot ones.
2. **The bot must be logged out of `api.telegram.org`.** Telegram gives no
   delivery guarantee to a local server otherwise, and refuses `logOut` while a
   webhook is set. `scripts/cookiebot_bot.py` in the infrastructure repo does
   the delete-then-logout dance once, behind a confirmation.

### Secrets

The chart never creates a Secret holding a credential; it only references one.
`secrets.name` (default `cookiebot-secrets`) must carry:

| Key | Used by |
|---|---|
| `bot-tokens` | `CB_BOT_TOKENS` — `{"skin": "123:ABC"}` |
| `webhook-secret` | `CB_WEBHOOK_SECRET`, checked on every update |
| `telegram-api-id`, `telegram-api-hash` | the Bot API server |
| `bot-token` | tooling only |

`CB_PG_DSN` comes from the Secret CloudNativePG generates (`<release>-citus-app`).

### GitOps

The infrastructure repo pins the chart through a multi-source Argo Application:
chart from this repository, values from that one. See
`gitops/apps/cookiebot-uat.yaml` and `gitops/workloads/cookiebot-uat/values.yaml`
in `home-self-hosted`.

## Four things that only a real build catches

Every one of these passed a syntax check and failed on first run. They are
called out because each has a guard in the Dockerfile now, and removing the
guard reintroduces the bug.

1. **`addgroup nonroot` fails** — `wolfi-base` already defines `nonroot` at
   65532. Just `USER 65532:65532`.
2. **Deleting `.py` without `compileall -b` breaks every import.** Python does
   not load `__pycache__/mod.cpython-313.pyc` once `mod.py` is gone; sourceless
   imports need the `.pyc` *at the source path*, which is what `-b` writes. The
   first image built cleanly and could not import its own launcher.
3. **uv installs workspace members as editable** — a `.pth` pointing at `/src`,
   which the runtime stage does not have. `import cb_core` worked in the builder
   and failed in the shipped image. Hence `--no-editable`, and hence the import
   check that runs *in the runtime stage*: the builder cannot catch this, it
   still has `/src` on disk.
4. **`Cython` is a runtime dependency, not a build one.** `cb_core/dedupe.py`
   and `textmatch.py` are written in pure-Python mode and `import cython` at
   import time. Deleting the package saves 11 MB and breaks every import of
   `cb_core.storage`.

Plus one in the launcher: granian 2.x takes `loop=Loops.auto`, not `loop_opt`.
An unknown keyword raises `TypeError` in the constructor — at startup, in the
cluster, after a green build.

## Local sanity checks

```sh
helm lint deploy/helm/cookiebot
helm template cookiebot deploy/helm/cookiebot -n cookiebot-uat | kubectl apply --dry-run=client -f -

podman build --build-arg SERVICE=cb-api -t cb-api:test .
podman run --rm --entrypoint /opt/cb/bin/python cb-api:test -c "import cb_launcher, cb_core"
podman images localhost/cb-api:test --format '{{.Size}}'
```

End to end, with real dependencies:

```sh
podman network create cbnet
podman run -d --name pgtest --network cbnet \
  -e POSTGRES_USER=cookiebot -e POSTGRES_PASSWORD=cookiebot -e POSTGRES_DB=cookiebot \
  postgres:17-alpine
podman run -d --name vktest --network cbnet valkey/valkey:8-alpine

podman run -d --name cbtest --network cbnet -p 18000:8000 \
  -e CB_SERVICE=cb-api \
  -e CB_PG_DSN=postgresql://cookiebot:cookiebot@pgtest:5432/cookiebot \
  -e CB_REDIS_DSN=redis://vktest:6379/0 \
  -e CB_AUTO_MIGRATE=false -e CB_TRACES_ENABLED=false cb-api:test

curl -s localhost:18000/healthz   # {"status":"ok","service":"cb-api","cython":true}
curl -s localhost:18000/readyz    # {"ready":true,"postgres":true,"valkey":true}

podman rm -f cbtest pgtest vktest && podman network rm cbnet
```

`CB_SERVICE=migrate` needs the `citus` extension, so it only completes against
the `cnpg-citus` image — against plain Postgres it fails at
`CREATE EXTENSION citus` and **exits 1**, which is what makes it usable as an
init container that blocks a bad rollout.

If a build dies with `no space left on device`, it is the podman VM, not your
disk: `podman system prune -f` (that also removes stopped containers and
dangling images).
