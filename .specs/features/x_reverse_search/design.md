# x_reverse_search — Design

Follows `util_youtube`'s split exactly: the gateway does the free, synchronous
parts and enqueues; the worker makes the external call and sends the reply.
Only what differs from that precedent is spelled out.

## R1 — the split

**R1.1** Gateway, `handlers/reverse_search.py`: the `utility` gate via
`deny_if_disabled` (v1 replies `utility_off` rather than ignoring —
`filters.py`'s `FeatureGate` docstring), then the reply check, then resolving
the replied message's `file_id` (largest photo, else document, else the
`reverse_image` string — D-RS-5). Enqueues `jobs.REVERSE_SEARCH` with
`group_id`, `message_id`, `file_id`, `lang`. Scalars only, as with every other
job.

**R1.2** Worker, `cb_worker/jobs/reverse_search.py`: downloads the file with
`bot.download(file_id)` (the idiom `transcribe.py:73-86` and `fun_random.py`
already share), POSTs the bytes to SauceNAO, and sends the reply and reaction
itself.

## R2 — the token leak (D-RS-1)

**R2.1** v1 hands SauceNAO a URL containing the bot token. v2 never constructs
that URL: the worker downloads through `bot.download()` and uploads the bytes
as a multipart `file` part. SauceNAO's REST API accepts either `url=` or a
file upload; only one of them leaks a credential.

**R2.2** This is also why the download belongs in the worker rather than the
gateway: the bytes are the payload, so fetching them where they are used avoids
putting an image body on the queue. arq payloads stay scalar.

## R3 — the API call

**R3.1** Direct `httpx` POST to `https://saucenao.com/search.php` with
`api_key`, `output_type=2` (JSON), `db=999` (all indexes), `numres=1`, and the
file part. Not `saucenao_api`: v2 already has `httpx` for every outbound call
(AGENTS.md §5), and this is one POST with four form fields — the same call
`util_youtube` made against `google-api-python-client`.

**R3.2** The two rate limits are fields on the response, which is how
`saucenao_api` distinguishes them too: `header.short_remaining < 0` ⇒
`reverse_other`, `header.long_remaining < 0` ⇒ `reverse_limit`. Checked in that
order, matching v1's `except` order (`:121-128`).

**R3.3** Result shape. `results[0].header.similarity` (a string percentage),
`results[0].data.ext_urls`, `data.title`, and the author under whichever of
`author_name` / `member_name` / `creator` is present — `saucenao_api` normalises
across indexes and v2 has to do the same, or an author present in v1's output
would silently vanish.

**R3.4** Everything else — non-2xx, a timeout, malformed JSON, an empty
`results`, a missing key — is `reverse_no_found` (D-RS-3). Request-level
failures are logged; a genuine no-match is not, the distinction `admins.py`
already draws between an outage and a real answer.

**R3.5** Timeout `settings.saucenao_timeout_seconds`, default 15.0 — higher
than YouTube's 5.0 because SauceNAO is fetching and hashing an image, not
answering from an index. No key configured ⇒ `reverse_no_found`, same as
`util_youtube`'s empty-key path.

## R4 — output

**R4.1** Threshold and indexing are v1's: `similarity > 80` (strict), and only
`results[0]` is ever considered, even when a later result would clear the bar
(`:129`). `numres=1` makes that explicit rather than incidental.

**R4.2** The answer is assembled exactly as v1 does (`:131-136`):
`reverse_best` + `f'"{title}"'` + `f" - {author}"` when there is one +
`f"\n{urls[0]}\n\n"` — trailing newlines included. **Not** re-translated
(D-RS-4).

**R4.3** Reactions via `bot.set_message_reaction(..., is_big=False)`, the
Bot API call `util_youtube`'s job already uses for the same reason (the job has
a `message_id`, not a live `Message`): `🫡` on a hit, `🤷` on a miss, both
best-effort suppressed.

## R5 — telemetry

**R5.1** `cb_worker_reverse_search_total{outcome}`, outcome in
`found|not_found|rate_limited|error`. No group id, no file id (AGENTS.md §7).

## Open decisions — answered

1. **Upload bytes, never a URL.** R2.1 — D-RS-1 is a credential leak and the
   only reason the port would otherwise be mechanical.
2. **REST over `httpx`, not `saucenao_api`.** R3.1, `util_youtube`'s precedent.
3. **Rate limits read off the response header, not exception types.** R3.2.
4. **Every other failure degrades to `reverse_no_found`.** R3.4 — no invented
   string.
5. **The double machine-translation is not ported.** D-RS-4.
