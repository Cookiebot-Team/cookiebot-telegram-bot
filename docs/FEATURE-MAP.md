# Cookiebot — Feature Traceability Map (v1 as-built)

Links every QA scenario (`Cookiebot-QA/features/*.feature`) to the code that implements it in
`COOKIEBOT-Telegram-Group-Bot/Bot/*` and `COOKIEBOT-backend`.

Legend: **QA** = spec exists · **BOT** = python handler · **API** = backend endpoint/collection.

---

## 1. Core (moderation / config) — 10 specs, 22 scenarios

| QA feature | Trigger (spec) | Trigger (code) | Bot handler | Backend | Status |
|---|---|---|---|---|---|
| `core_botskins` | 3 skins | 5 personas via `is_alternate_bot` 0-4 | `universal_funcs.py:39-52` `get_bot_token` | — | ⚠ skins = separate OS processes + tokens, not a runtime config |
| `core_groupguardian` | join captcha | join event | `GroupShield.py:231-265` `captcha_message`, `:313-344` `solve_captcha` | `configs.timeCaptcha` | ⚠ state in flat `Captcha.txt`, no real lock |
| `core_listcommand` | `/commands` | `/comandos`,`/commands` | `Miscellaneous.py:124-127` | — | ✅ (reads `Static/locales/*/Cookiebot_functions.txt`) |
| `core_mediarestrict` | new-user media block | photo/video handlers | `COOKIEBOT.py:167-172` + `configs.timeWithoutSendingImages` | `GET /configs/{id}` | ✅ |
| `core_musicdetection` | voice w/ music | voice message | `Audio.py:6-20` `identify_music` (ShazamAPI) | — | ⚠ unofficial reverse-eng API |
| `core_privacy` | `/privacy` | `/privacy`,`/privacidade`,`/privacidad` | `Miscellaneous.py:60-63` | — | ✅ |
| `core_rules` | `/rules`,`/newrules` | same | `GroupShield.py:49-63`, `Configurations.py:281-283`,`:269-279` | `GET/PUT /rules/{id}` | ✅ |
| `core_setlang` | web settings page | `/configurar` menu | `Configurations.py:242-251` `set_language` | `configs.language`, `PUT /bff/group/{id}/config` | ⚠ spec says web UI, bot does in-chat menu |
| `core_stickerspam` | sticker flood | sticker event | `Cooldowns.py:12-22` `sticker_anti_spam` | `configs.stickerSpamLimit` | ⚠ counter in unlocked process-local dict |
| `core_welcome` | `/newwelcome` + join | same | `GroupShield.py:140-171`, `Configurations.py:265-267`,`:253-263` | `GET/PUT /welcomes/{id}` | ✅ |

## 2. Fun — 9 specs, 21 scenarios

| QA feature | Trigger (spec) | Trigger (code) | Bot handler | Backend | Status |
|---|---|---|---|---|---|
| `fun_battle` | `/battle` | `/batalha`,`/battle`,`/batalla` | `SocialContent.py:294-379` | — | ⚠ scrapes `telegram.me` HTML for avatars (`:279-292`) |
| `fun_complaint` | `/complaint` | `/milton`,`/reclamacao`,`/complaint`,`/queja` | `Miscellaneous.py:240-248`,`:250-259` | — | ✅ |
| `fun_death` | `/death` | `/morte`,`/muerte`,`/death` | `Miscellaneous.py:335-357` | — | ✅ |
| `fun_dice` | `roll 6` / `roll 20` | `/dado`,`/dice`,`/d<N>` | `Miscellaneous.py:160-183` | — | ❌ **spec/code trigger mismatch** |
| `fun_firecracker` | `/firecracker` | `/rojao`,`/acende`,`/fogos`,`/firecracker` | `Miscellaneous.py:226-238` | — | ✅ |
| `fun_meme` | meme cmd | `/meme` | `SocialContent.py:224-277` | — | ✅ |
| `fun_partneredcons` | `/bff`,`/patas`,`/fursmeet`,`/trex`,`/furcamp`,`/pawstral` | `/patas`,`/bff`,`/furcamp`,`/fursmeet`,`/pawstral` | `Miscellaneous.py:261-323` `event_countdown` | — | ❌ **`/trex` spec'd, not implemented** |
| `fun_random` | `/random` | `/aleatorio`,`/random` | `SocialContent.py:198-206` | `GET/POST /randomdatabase` | ⚠ backend loads whole collection to pick 1 |
| `fun_ship` | `/shipp` | `/shippar`,`/ship` | `UserRegisters.py:216-250` | `GET /registers/{id}/users` | ✅ |

## 3. Util — 12 specs, 18 scenarios

| QA feature | Trigger (spec) | Trigger (code) | Bot handler | Backend | Status |
|---|---|---|---|---|---|
| `util_birthday` | `/birthday` | `/aniversário`,`/birthday`,`/cumpleaños` | `Birthdays.py:14-61` | `GET /users?birthdate=` | ⚠ `$expr` month/day query = full scan, unindexed |
| `util_calladms` | `/adm` | `/adm`,`@admin`,`/report` | `UserRegisters.py:168-176`,`:178-203` | — | ✅ |
| `util_config` | `/config` | `/configurar`,`/configure` | `Configurations.py:139-167`,`:213-240` | `GET/PUT /configs/{id}` | ❌ **trigger mismatch** (`/config` vs `/configurar`) |
| `util_deletereposts` | `/deletereposts` | `/deleteposts`,`/apagarposts` | `Publisher.py:316-327` | local `Publisher.db` | ❌ **trigger mismatch** |
| `util_doomlist` | join gate | join event | `GroupShield.py:172-229` `check_cas`/`check_banlist`/`check_banlist_public` | `GET /blacklist/{id}`, ext `api.cas.chat`, ext `burrbot.xyz` | ⚠ 2 external deps in join hot path |
| `util_embedder` | social link | any message | `SocialContent.py:79-84` `check_reply_embed` | — | ✅ |
| `util_everyone` | `/ping everyone` | `/everyone`,`@everyone` | `UserRegisters.py:97-146` | `GET /registers/{id}/users` + per-user `GET /users?username=` | ❌ trigger mismatch; ⚠ **N+1 backend calls** |
| `util_isalive` | `/isalive` | `/tavivo`,`/isalive` | `Miscellaneous.py:65-69` | — | ✅ |
| `util_nextbirthday` | `/nextbirthday` | `/proximosaniversarios`,`/nextbirthdays` | `Birthdays.py:104-117` | `GET /users` | ⚠ plural/singular mismatch |
| `util_postforwarder` | cross-group forward | `/divulgar`,`/publish`,`/publicar` | `Publisher.py:46-92`,`:223-286` | `configs.publisherPost/Ask/MembersOnly` | ⚠ **shared unlocked SQLite conn** `Publisher.py:15-16` |
| `util_postgetter` | receive forwarded | channel-forward event | `Publisher.py:46-55` `ask_publisher` | `configs.threadPosts`,`maxPosts` | ✅ |
| `util_youtube` | `/youtube <q>` | `/youtube` | `SocialContent.py:172-189` | — | ✅ (YouTube Data API) |
| `publicador(PTBR).md` | approval workflow | `/repost` scheduling | `Publisher.py:288-314`,`:329-357` scheduler | `Publisher.db` | ⚠ prose only, no Gherkin |

---

## 4. Implemented but **NOT** spec'd in QA (spec debt — 20+ features)

| Feature | Trigger | Code |
|---|---|---|
| Giveaways | `/giveaway` + 4 callbacks | `Giveaways.py:25-173`, `Giveaways.db` |
| Reverse image search | `/buscarfonte`,`/searchsource` | `SocialContent.py:113-142` (SauceNao) |
| Age guess | `/idade`,`/age` | `Miscellaneous.py:185-202` (agify.io) |
| Gender guess | `/genero`,`/gender` | `Miscellaneous.py:204-224` (genderize.io) |
| Unearth | `/desenterrar`,`/unearth` | `Miscellaneous.py:325-333` |
| Fortune cookie | `/sorte`,`/fortunecookie` | `Miscellaneous.py:359-375` |
| Destroy/distort | `/zoar`,`/destroy` | `Miscellaneous.py:377-432` + `Distortioner.py` |
| Image search | `/qualquercoisa`,`/anything` + fallback | `SocialContent.py:144-170` |
| Drawing idea | `/ideiadesenho`,`/drawingidea` | `Miscellaneous.py:137-143` |
| Analysis | `/analise`,`/analysis` | `Miscellaneous.py:71-81` |
| Sticker DB auto-reply | sticker/doc reply | `SocialContent.py:208-222` |
| Conversational AI | mention "cookiebot" / reply | `NaturalLanguage.py:65-77` (OpenAI) |
| Speech-to-text | voice msg | `Audio.py:22-32` (Whisper API) |
| Custom commands | GCS `Custom/` prefix | `Miscellaneous.py:145-158` |
| Owner: list groups | `/grupos` | `Miscellaneous.py:83-112` |
| Owner: broadcast | `/broadcast` | `Miscellaneous.py:114-122` |
| Owner: leave+blacklist | `/leave` | `universal_funcs.py:320-329` |
| Owner: (un)blacklist | `/blacklist`,`/unblacklist` | `universal_funcs.py:307-313` |
| Owner: stop / restart | `/stop`,`/restart` | `COOKIEBOT.py:89-94` |
| Reload caches | `/reload`,`/recarregar` | `COOKIEBOT.py:197-201` |
| WebHub JWT login | HTTP `POST /login` | `Server.py:25-52` (Telegram Login Widget → RS256 JWT) |

Backend-only, no bot/QA coverage: **Events CRUD + BFF** (`EventResource`, `bff/events`), **Groups/admins** (`GroupResource`), **`Raffle` domain orphan** (entity exists, no repo/service/controller — but `docs/openapi.json` still documents `/raffles/*` = stale generated spec).

## 5. Spec'd but **NOT** implemented

- `/trex` partnered-con command (`fun_partneredcons.feature:20-23`).
- `core_setlang` as a **web settings page** — only in-chat `/configurar` exists.
- Entire QA automation layer: `tests/` and `pages/` are empty `__init__.py`; `conftest.py` has an orphaned `@pytest.fixture` over a commented-out Playwright function. **61 scenarios, 0 executable.**

## 6. Cross-cutting defects worth carrying into v2 as regression tests

| # | Defect | Location |
|---|---|---|
| D1 | Write calls (`POST`/`PUT`/`DELETE`) share the 60s read memo-cache → duplicate writes silently no-op | `universal_funcs.py:106,117,128` |
| D2 | `verify=False` on every backend call → TLS validation disabled | `universal_funcs.py:100,111,122,133` |
| D3 | `while SEMAPHORE_VIDEOS: pass` busy-wait burns a core; serializes all distortion globally | `Distortioner.py:114-115,145-146,155-156` |
| D4 | Fixed temp filenames (`meme.png`,`CAPTCHA.png`,`temp.jpg`,`user1.jpg`…) shared across 50 threads → cross-chat file clobber | `SocialContent.py:275`, `GroupShield.py:242`, `Birthdays.py:92`, … |
| D5 | `Publisher.db` single shared conn, `check_same_thread=False`, no lock | `Publisher.py:15-16` |
| D6 | Caches unbounded + unlocked, never expire (5 processes = 5 divergent views) | `Configurations.py:9-12`, `UserRegisters.py:11-12`, `Cooldowns.py:8-10` |
| D7 | JWT signing key regenerated in memory every restart → all tokens invalidated | `Server.py:22-23` |
| D8 | `/grupos`, `/broadcast`, `birthday()` loop all groups with `sleep(0.4/0.5/3)` on a worker thread | `Miscellaneous.py:96-103,114-122`, `Birthdays.py:61` |
| D9 | `GroupResource.deleteAdmins` missing `@RequestBody` → body never binds | `GroupResource.java:135-139` |
| D10 | No index on `Event.groupId`, `User.username`, `User.birthdate`; `$expr` birthday query un-indexable | `Event.java`, `User.java`, `UserRepository.java:16` |
| D11 | Zero pagination — every list endpoint returns the full collection | all `*Service.findAll()` |
| D12 | `/actuator/health` + `/actuator/prometheus` are `.anonymous()` = public | `SpringSecurityConfig.java:56-64` |
| D13 | CORS `allowed-origins:"*"` + `allow-credentials:true` on `/bff/**` | `application.yml:2-12` |
