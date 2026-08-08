# x_webhub_login — Specify

**Feature id:** `x_webhub_login` · **Area:** platform · **Milestone:** M4 ·
**Kind:** v1 port with no QA scenario (the QA repo describes the bot, not the
web console).

## Goal

`COOKIEBOT-WebHub` signs a user in with Telegram's login widget and exchanges
the widget's payload for a JWT it then sends as `Authorization: Bearer` on
every call to the backend. v2 serves that exchange from `cb-api`, together with
the JWKS and OIDC discovery documents a resource server needs to verify the
token without calling back.

## Source of truth

`../COOKIEBOT-Telegram-Group-Bot/Bot/Server.py:1-95` — a Flask app run under
gunicorn on `:8080`: `validate_telegram_auth` (`:25-38`), `generate_jwt_token`
(`:40-52`), `POST /login` (`:58-76`), `GET /` (`:55-57`),
`GET /.well-known/jwks.json` (`:78-84`), `GET /.well-known/openid-configuration`
(`:86-95`). The client side is `../COOKIEBOT-WebHub/src/lib/api/axios.ts` and
`src/lib/auth/token.ts`.

## What the client actually depends on

Ported behaviour is defined by what `axios.ts` does, not by what looks right:

1. `POST /login` takes the widget payload as a flat JSON object and answers
   `{"accessToken": "<jwt>"}` (v1 also sends `"status": "Token generated"`).
2. A failure is `401 {"error": "Invalid bot token"}`; an empty body is
   `400 {"error": "Missing data"}`.
3. **The client renews by re-posting the same stored `telegramAuthData`**
   (`getOrRenewToken`), which it keeps in `localStorage` indefinitely. Its
   expiry check is `tokenData.exp < Date.now() / 1000` — note the missing
   `* 1000` on the other side, so it in fact only renews on a decode failure or
   a missing token.
4. The token is read by `jwt-decode` client-side, so `exp` must be a numeric
   claim.

## Four findings that shape the port

**1. The signing key is regenerated on every restart** (`Server.py:23-24`,
FEATURE-MAP **D7**). Every already-issued token becomes unverifiable, and the
JWKS the resource server cached now describes a key that no longer exists. With
gunicorn's two workers it is worse than v1's own authors could have seen: each
worker generates its **own** key, so which key signed a token depends on which
worker answered, and `/.well-known/jwks.json` publishes only the key of the
worker that happened to serve *that* request. Half of all tokens fail
verification at any moment.

**2. There is no replay window.** `validate_telegram_auth` never looks at
`auth_date` (`Server.py:25-38`), so a captured widget payload mints new tokens
forever. Telegram's own documentation says to check it. The catch is finding 3
above: the shipped WebHub re-posts a payload it stored at first login, so
enforcing freshness logs every existing session out as soon as its token
expires. Ported as **off by default** (`CB_WEBHUB_AUTH_MAX_AGE_SECONDS=0`
reproduces v1 exactly) with the enforcement written, tested and one setting
away — see "Open decision" below.

**3. The issuer is whatever the `Host` header says.** `request.url_root`
(`Server.py:71,88`) behind a `ProxyFix` that trusts `X-Forwarded-Host` means
anyone who can reach the service can make it mint a token whose `iss` names a
host they chose, and publish a discovery document pointing at it.

**4. The bot tokens are five hardcoded `os.getenv` names** (`Server.py:62-68`)
tried in order. v2 already has `settings.bot_tokens`, a skin -> token map that
`core_botskins` populates, so the same "any of our bots' widgets may sign you
in" behaviour comes from configuration rather than from a literal list.

## Decisions

| # | Decision | Why |
|---|---|---|
| R1 | The RSA key is loaded from `CB_WEBHUB_JWT_PRIVATE_KEY_PEM` if set; otherwise generated **once** and persisted in a `signing_keys` reference table | Fixes D7 for every deployment, including one that configures nothing — which is the deployment D7 actually bites. An operator who would rather the key never touched the database sets the env var and the table is never read. |
| R2 | `kid` stays `cookiebot-2025` by default | v1's literal (`Server.py:23`). A resource server that pinned it keeps working. |
| R3 | `iss` comes from `CB_WEBHUB_ISSUER`; unset falls back to the request's own base URL | Finding 3 is only fixable by configuration — v2 cannot know its public URL. The fallback preserves v1's behaviour so nothing breaks unset; the setting is what closes the hole. |
| R4 | Hash comparison is `hmac.compare_digest` | v1 used `==` on a hex string. Not a demonstrated exploit here, but it costs one import. |
| R5 | `GET /` reports the real group count | v1 answered a hardcoded `NUMBER_CHATS = 1275` (`Server.py:17`) that no code ever updated. |
| R6 | 30-minute TTL, `RS256`, claims `exp`/`iat`/`kid`/`sub`/`iss` | v1's own token, field for field, including `kid` as a *claim* (unusual — it belongs in the header, where this port also puts it, and v1's `jwt.encode` already did). |
| R7 | `/login`'s CORS is an explicit origin allowlist | v1 shipped `origins: "*"`. AGENTS.md D13, and the app-level middleware already works this way. |
| R8 | No new dependency | `PyJWT` + `cryptography` are already resolved in the workspace; `jwcrypto` (v1's) is not needed for one RSA key. |

## Open decision — for the owner

**Should `CB_WEBHUB_AUTH_MAX_AGE_SECONDS` default to 0 (v1's replay window,
which is forever) or to a real value?** Anything non-zero logs out every WebHub
session whose stored payload is older than the window, because the client's
renewal path re-posts that payload rather than re-running the widget. Shipping
it off preserves compatibility; shipping it on requires a WebHub change first
(re-run the login widget on a 401 instead of replaying). The code is written
either way — this is a one-line default.

## Success criteria

1. A payload signed with any configured bot token mints a token; one signed
   with an unknown token gets v1's `401`.
2. Two processes with no configured PEM converge on the **same** key, and the
   key survives a restart — the D7 regression test.
3. `/.well-known/jwks.json` verifies a token issued by `/login`, end to end.
4. Every v1 response shape is byte-compatible for the fields `axios.ts` reads.
5. `ruff`, `mypy` and `cb.py check` clean.
