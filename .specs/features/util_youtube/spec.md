# util_youtube — Specify

**Feature id:** `util_youtube` · **Milestone:** M2 · **Kind:** v1 port
**v1 source:** `Bot/SocialContent.py:172-189` (`youtube_search`), dispatched
`Bot/COOKIEBOT.py:248-249,260-261`.

## Goal

`/youtube <query>` searches YouTube and posts a random pick from the top 10
results. No blocker: no bucket asset, no dead code, no scope ambiguity.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | `/youtube` — shared `elif` chain with `/dado`, `/ideiadesenho`, `/giveaway` (`COOKIEBOT.py:248-249,260-261`), gated on `utilityfunctions` (`:252-253`, `notify_utility_off` when off — **not** `functionsFun`) |
| Preconditions | `utilityfunctions` only. No admin check, no cooldown (grepped `Cooldowns.py`, no entry). |
| No query | `len(msg['text'].split()) == 1` ⇒ `youtube_need` ("You need to type the name of the video\n<blockquote> EXAMPLE: /youtube baked potato </blockquote>") and return — no API call at all (`:173-176`) |
| Search | `youtubesearcher.search().list(q=query, part="snippet", type="video", maxResults=10)` — YouTube Data API v3, `query = ' '.join(msg['text'].split()[1:])` (everything after the command word, space-joined) (`:177-179`) |
| No results | react `🤷` (`is_big=False`) then `youtube_no_find` ("I couldn't find any video") (`:181-184`) |
| Success | `random.choice(videos)` from up to 10 results; reply (`msg_to_reply=msg`) `f"<i> {video_url} </i>\n\n<b> {video_description} </b>"`, `parse_mode='HTML'` — `video_url` is `https://www.youtube.com/watch?v={videoId}`, `video_description` is the raw YouTube snippet description, unescaped/untruncated (`:186-189`) |
| Persistence | None |
| External calls | YouTube Data API v3 `search.list`, no timeout set in v1 (`googleapiclient`'s own default, effectively unbounded) |
| Known defects | D-YT-1 below |

## Defects — verdict per item

| id | Defect | Verdict |
|---|---|---|
| D-YT-1 | Synchronous, unbounded external API call directly on the reply path — a slow or hanging YouTube API response blocks the handler thread (v1's whole architecture is threaded workers, so this blocks one of them; v2's single async event loop per replica would be worse, blocking everyone behind it). | **fix** — AGENTS.md §2.4 names an external API call as exactly the "enqueue to cb-worker" case. Gateway does the free, synchronous parts (the gate, the no-query check) and enqueues everything that touches YouTube. |

## Design note — reply timing changes, necessarily

Moving the search off the reply path means the eventual `sendMessage` (success
or `youtube_no_find`) happens from `cb-worker`, a network hop and a queue
after the group message that triggered it — the same shape `util_everyone`'s
fan-out and `util_calladms`'s DM already established for "the answer is a
second call from a different process." v1's single-message output is
unchanged; only which process sends it and exactly when.

## QA

`../Cookiebot-QA/features/util_youtube.feature` — one scenario, matches v1
exactly: `/youtube how to make a cake` → "a link to a youtube video about
how to make a cake." Copied wording-unchanged. No conflict to record.
