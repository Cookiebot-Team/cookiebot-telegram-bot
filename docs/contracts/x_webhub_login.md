# Contract: x_webhub_login (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the web console's Telegram-login token
exchange. **No QA scenario exists and none is authored** — the QA repo
describes the bot's behaviour in Telegram, and this feature has no Telegram
surface at all. Its acceptance bar is the HTTP contract, tested at the unit and
integration layers. FEATURE-MAP row: `x_webhub_login`. Spec:
`.specs/features/x_webhub_login/spec.md`.

Files owned by this port:
`packages/cb-api/migrations/versions/0008_signing_keys.py` (new),
`packages/cb-api/src/cb_api/auth.py` (new),
`packages/cb-api/src/cb_api/keys.py` (new),
`packages/cb-api/src/cb_api/routers/login.py` (new),
`packages/cb-api/src/cb_api/main.py` (registration, CORS),
`packages/cb-api/pyproject.toml` (`pyjwt[crypto]`),
`packages/cb-core/src/cb_core/settings.py` (the `webhub_*` block), and the
tests listed at the bottom.

## Phase 1 — where v1 lives

- Service: `../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py` — Flask behind
  `ProxyFix`, run by gunicorn on `0.0.0.0:8080` with **two sync workers**
  (`:110-118`), started from the bot process itself.
- `validate_telegram_auth`: `:25-38`. `generate_jwt_token`: `:40-52`.
- Routes: `GET /` (`:55-57`), `POST /login` (`:58-76`),
  `GET /.well-known/jwks.json` (`:78-84`),
  `GET /.well-known/openid-configuration` (`:86-95`).
- Key material: `jwk.JWK.generate(kty='RSA', size=2048, alg='RS256',
  use='sig', kid='cookiebot-2025')` at import (`:23-24`).
- Client: `../COOKIEBOT-WebHub/src/lib/api/axios.ts` and
  `src/lib/auth/token.ts`.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| `GET /` | `{"status": "Bot is online", "number_chats": 1275}` — a module constant (`:17,55-57`) |
| `POST /login` body | the login widget's flat JSON payload |
| Empty body | `400 {"error": "Missing data"}` (`:61-62`) |
| Signature check | pop `hash`; `"\n".join(f"{k}={v}")` over the remaining keys in sorted order; HMAC-SHA256 keyed by `sha256(bot_token)`; `==` against the popped hash (`:31-37`) |
| Which tokens | five `os.getenv` names in a list, tried in order (`:62-68`) |
| Success | `{"status": "Token generated", "accessToken": <RS256 JWT>}` (`:71-75`) |
| Failure | `401 {"error": "Invalid bot token"}` (`:76`) |
| Claims | `exp = round(time())+1800`, `iat = round(time())`, `kid`, `sub = data['id']`, `iss = request.url_root.rstrip('/')` (`:41-48,71`) |
| `auth_date` | **never examined** |
| JWKS | `{"keys": [<the one key this worker generated>]}` (`:80-84`) |
| Discovery | `issuer`, `jwks_uri`, `response_types_supported: ['id_token']`, `subject_types_supported: ['public']`, `id_token_signing_alg_values_supported: ['RS256']`, all off `request.url_root` (`:86-95`) |
| CORS | `CORS(app, resources={r"/login": {"origins": "*"}})` (`:22`) |
| Known defects | D-WL-1 … D-WL-6 below |

## What the client depends on

From `axios.ts`, and therefore not negotiable:

- `POST /login` -> `data.accessToken`.
- The token is decoded client-side with `jwt-decode`, so `exp` must be numeric.
- **Renewal re-posts the `telegramAuthData` saved in `localStorage`** at first
  login (`getOrRenewToken` -> `loginAndSaveToken`), indefinitely. This is why
  D-WL-3's fix ships switched off.
- Its own expiry test is `tokenData.exp < Date.now() / 1000` — seconds against
  milliseconds, so it is effectively never true and the client renews only when
  the token is missing or fails to decode. Not v2's bug to fix, but it is the
  reason a shortened TTL would be invisible until a browser reload.

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-WL-1 | **The signing key does not survive the process, and there are two of them.** Generated at import (`Server.py:23-24`) under two gunicorn workers (`:112`): each worker signs with its own key and `/.well-known/jwks.json` publishes only the key of the worker that served *that* request, so a consumer that fetches the JWKS and verifies a token gets the wrong key about half the time. Every restart invalidates every token already issued. FEATURE-MAP **D7**. | **fix** — `CB_WEBHUB_JWT_PRIVATE_KEY_PEM`, else one row in `signing_keys` (migration `0008`, reference table) generated once and read by every replica. The JWKS publishes every row, so rotation can overlap. `qa/integration/test_webhub_login.py` is the regression. |
| D-WL-2 | **Only the first configured bot could ever sign anyone in.** `validate_telegram_auth` does `auth_data.pop('hash', None)` (`:32`) on the caller's dict, and the caller loops over five tokens with that same dict (`:69-70`). The second iteration sees a payload with no `hash` and returns `False` immediately. Four of the five personas' users got `401` no matter what. | **fix** — the payload is not mutated; every configured skin's token is tried. `test_the_payload_is_not_mutated` pins it. |
| D-WL-3 | **No replay window.** `auth_date` is never checked (`:25-38`), so a captured widget payload mints fresh tokens forever. | **fixed, off by default** — `CB_WEBHUB_AUTH_MAX_AGE_SECONDS`, `0` reproducing v1. Turning it on logs out every session whose stored payload predates the window, because the client renews by replaying it (see above). Enabling it is a WebHub change first; the decision is recorded in the spec. |
| D-WL-4 | **`iat` can be one second in the future.** `round(time.time())` (`:42,46`) rounds up half the time; a verifier that checks `iat` — PyJWT does, with no leeway by default — rejects the token it was just handed. | **fix** — floored. `exp` is still `iat + ttl`, so a token's life changes by at most that second. |
| D-WL-5 | **The issuer is caller-controlled.** `request.url_root` (`:71,88`) behind `ProxyFix(x_host=1)` is `X-Forwarded-Host`, so anyone who can reach the service picks the `iss` its tokens carry and the `jwks_uri` its discovery document advertises. | **fixable by configuration only** — `CB_WEBHUB_ISSUER`. Unset, v1's behaviour is reproduced exactly, because v2 cannot know its own public URL. Documented in `.env.example` as a value to set. |
| D-WL-6 | **`CORS(origins="*")` on `/login`** (`:22`). | **fix** — `CB_WEBHUB_ALLOWED_ORIGINS`, an explicit allowlist, through the app-level middleware that already worked this way (AGENTS.md D13). |
| (minor) | `NUMBER_CHATS = 1275` was a literal nothing updated (`:17`). | **fix** — `GET /` counts `groups`. |
| (minor) | `==` on the hex digest (`:38`). | **fix** — `hmac.compare_digest`. |

## Preserved deliberately

- **Every response shape**, field for field, including `"status": "Token
  generated"` and both error bodies — the client branches on the status codes
  and reads `accessToken` by name.
- **`kid` as a payload claim**, which is unusual (it belongs in the header,
  where `jwt.encode` also put it, and where this port puts it too). A consumer
  written against v1 may be reading it.
- **`kid = "cookiebot-2025"`** as the default (`Server.py:23`).
- **RS256, 2048-bit, 30-minute TTL.**
- **The discovery document's exact field set**, in v1's order.
- **The `iss` fallback to the request's base URL** when nothing is configured.

## Not ported

`kill_api_server()` (`Server.py:120-127`) walks the process table for anything
listening on 8080 and kills it. It exists only to serve `/stop` and `/restart`,
which `x_owner_commands` does not port either — same reason, written up in
`docs/contracts/x_owner_commands.md`.

`run_api_server`'s gunicorn embedding (`:110-118`) is gone by construction:
`cb-api` is its own service with its own lifecycle, and never a thread inside
the bot.

## Operational note

`signing_keys` holds an unencrypted RSA private key when no PEM is configured.
That is a deliberate trade-off and migration `0008`'s docstring argues it: the
alternative for an unconfigured deployment is not "no key at rest", it is D7 —
a different key per replica per restart. `CB_WEBHUB_JWT_PRIVATE_KEY_PEM` is the
supported way to keep the key out of the database entirely, and when it is set
the table is never read or written.

## Phase 6 — parity

| Behaviour | v1 | v2 | Same? |
|---|---|---|---|
| `POST /login` success body | `{status, accessToken}` | same | ✅ |
| Error bodies and codes | `400`/`401` with those exact strings | same | ✅ |
| Signature algorithm | Telegram's, over sorted `k=v` lines | same | ✅ |
| Tokens accepted | five env vars, **first only** in practice | every configured skin | ⚠️ D-WL-2 |
| Claims | `exp`/`iat`/`kid`/`sub`/`iss` | same, `iat` floored | ⚠️ D-WL-4 |
| TTL | 1800s | same, configurable | ✅ |
| Signing key | per process, per restart | one, persisted, shared | ⚠️ D-WL-1 |
| JWKS | one key — this worker's | every key in the table | ⚠️ D-WL-1 |
| Discovery document | v1's fields | same | ✅ |
| `iss` source | `request.url_root` | setting, falling back to it | ⚠️ D-WL-5 |
| `auth_date` | ignored | ignored by default, enforceable | ⚠️ D-WL-3 |
| CORS on `/login` | `*` | allowlist | ⚠️ D-WL-6 |
| `GET /` count | hardcoded 1275 | real | ⚠️ minor |
| Transport | Flask + gunicorn inside the bot process | FastAPI in `cb-api` | ⚠️ by design |

## Tests

| Layer | File |
|---|---|
| Unit | `packages/cb-api/tests/test_webhub_auth.py` — the widget signature against independently computed vectors, the non-mutation D-WL-2 pins, the freshness window, v1's claim set, and a token verifying against its own published JWK |
| Unit | `packages/cb-api/tests/test_login_endpoints.py` — the four endpoints over HTTP: both error bodies, any-skin login, the JWKS round trip, no private material in the JWKS, the discovery document, and the issuer fallback |
| Integration | `qa/integration/test_webhub_login.py` — real Citus: the key is written once, survives a "restart", a token issued before one still verifies after it, a replica losing the insert race adopts the winner's key, a configured PEM never touches the table, and `signing_keys` really is a reference table |
