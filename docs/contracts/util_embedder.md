# Contract: util_embedder (v1 -> v2)

Phase 2/6 of `/migrate-feature` for the social-link embedder. QA:
`../Cookiebot-QA/features/util_embedder.feature`. FEATURE-MAP row:
`util_embedder` (`social link`, any message, `SocialContent.py:79-84`
`check_reply_embed`, no backend calls, "✅"). Files owned by this port:
`packages/cb-gateway/src/cb_gateway/handlers/embedder.py`,
`qa/features/util_embedder.feature`, `qa/test_util_embedder.py`,
`packages/cb-gateway/tests/test_embedder.py`, this file.

## Phase 1 — v1 source

```python
# Bot/SocialContent.py:26-29
TWITTER_REGEX = r"(?:twitter|x)\.com/[a-zA-Z0-9_]{1,15}/status/[0-9]{1,20}"
TIKTOK_REGEX = (
    r"(?:tiktok\.com/@[a-zA-Z0-9_.]{1,24}/video/[0-9]{1,20}|vm\.tiktok\.com/[A-Za-z0-9]+/?)"
)
INSTAGRAM_REGEX = r"instagram\.com/(reel|p)/[a-zA-Z0-9_-]{1,11}"
BSKY_REGEX = r"bsky\.app/profile/[a-zA-Z0-9.-]{1,253}"


# Bot/SocialContent.py:49-77
def fix_embed_if_social_link(message: str) -> str | bool:
    message = message.strip()
    if any(
        domain in message
        for domain in [
            "vxtwitter.com",
            "fxtwitter.com",
            "fixupx.com",
            "d.tnktok.com",
            "vm.vxtiktok.com",
            "ddinstagram.com",
            "kkinstagram.com",
            "fxbsky.app",
        ]
    ):
        return False
    try:
        requests.get(message, timeout=2)
    except:
        return False
    transformations = [
        (TWITTER_REGEX, "https://fixupx.com/{}", r"[^/]+/status/[0-9]+"),
        (TIKTOK_REGEX, "https://vm.vxtiktok.com/{}", r"@[^/]+/video/[0-9]+"),
        # (INSTAGRAM_REGEX, "https://kkinstagram.com/{}", r'(reel|p)/([^?/]+)'),   # <- disabled
        (BSKY_REGEX, "https://fxbsky.app/profile/{}", r"\.app/profile/(.+)"),
    ]
    if re.search(TIKTOK_REGEX, message) and re.search(
        r"vm\.tiktok\.com/.+|tiktok\.com/t/.+", message
    ):
        try:
            message = requests.get(message, timeout=1).url  # follow short-link redirect
        except:
            return False
    for main_pattern, template, extract_pattern in transformations:
        if re.search(main_pattern, message):
            if match := re.search(extract_pattern, message):
                return template.format(match.group(1) if "(" in extract_pattern else match.group())
            return False
    return False


# Bot/SocialContent.py:79-84
def check_reply_embed(cookiebot, msg, chat_id, is_alternate_bot):
    if "link_preview_options" not in msg:
        return
    url_embed = fix_embed_if_social_link(msg["text"])
    if url_embed and url_embed.strip() != msg["text"].strip():
        send_message(
            cookiebot,
            chat_id,
            url_embed,
            msg_to_reply=msg,
            is_alternate_bot=is_alternate_bot,
            link_preview_options=json.dumps(
                {
                    "show_above_text": True,
                    "prefer_large_media": True,
                    "disable_web_page_preview": False,
                }
            ),
        )
```

Call site, `Bot/COOKIEBOT.py`:

```python
# :106-110 — private chats return before this code is ever reached
if chat_type == 'private':
    ...
    return
...
# :185 — every remaining branch below is inside `elif 'text' in msg:`
elif 'text' in msg:
    if msg['text'].startswith("/") and len(msg['text']) > 1:
        ...                                    # every command lives in this sibling branch
    elif ...welcome-prompt reply...:            # :290
    elif ...rules-prompt reply...:              # :293
    elif ...cookiebot "who" trigger...:         # :296
    elif ...captcha photo reply...:             # :298
    elif ...milton complaint-prompt reply...:   # :300
    elif ...bot message with reply_markup...:   # :302
    elif ...conversational-AI trigger...:       # :304
    else:                                        # :309-316 — the catch-all this feature lives in
        if 'from' in msg:
            if utilityfunctions:
                check_reply_embed(cookiebot, msg, chat_id, is_alternate_bot)
            increase_remaining_responses_ai(msg['from']['id'])
        if captchatimespan > 0 and myself['username'] in listaadmins:
            solve_captcha(...)
            check_captcha(...)
```

`utilityfunctions` default: `Configurations.py:111` — `..., funfunctions, utilityfunctions, ... = 1, 1, ...` → **on** by default, matching `GroupConfig.functions_utility = True` (`cb_core/group_config.py:57`) — no drift.

## Phase 2 — v1 behaviour contract

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | Any ordinary group message that falls all the way through to the dispatcher's trailing `else` (`COOKIEBOT.py:309`) — i.e. **not** a command (`.startswith('/')`, checked whether or not it parses as a real command), **not** a reply to the welcome/rules/captcha/milton prompts, **not** the "who" trigger, **not** the conversational-AI trigger. Private chats never reach it at all (`:106-110`'s early return). |
| Preconditions | `if utilityfunctions:` only (`:311`) — v1's `functionsUtility` gate, default on. No admin check, no other precondition beyond `'from' in msg` (`:310`). `check_reply_embed` itself additionally requires `'link_preview_options' in msg` (a key Telegram includes on a message that carries a link) before calling into the rewrite logic at all. |
| Cooldowns / quotas | None. No per-user or per-group limit anywhere in this path. |
| Success output | **Reply** (`send_message(..., msg_to_reply=msg, ...)`, never edits, never deletes the original) containing **only the rewritten URL**, no extra text, with `link_preview_options={'show_above_text': True, 'prefer_large_media': True, 'disable_web_page_preview': False}` (`:84`). Skipped if the rewritten string equals the original message verbatim (`:83`'s `!=` check). |
| Failure output | Nothing. `fix_embed_if_social_link` returning `False` (unsupported host, unreachable/invalid "URL", no regex match) produces total silence — no error message, no reaction. |
| Persistence | None — this feature reads and writes nothing. |
| Side effects | None beyond the one reply. |
| External calls | `requests.get(message, timeout=2)` on the **entire message text** (not just the link) before any rewrite — see "v1 defect" below. A second `requests.get(message, timeout=1)` only for a TikTok short link, to follow the redirect and recover the long-form URL before extraction. |
| Known defects | Not a FEATURE-MAP D-item, but two real defects found while reading the source (below). |

### v1 defect #1 — `requests.get(message, ...)` on the whole message text

`requests.get` requires a complete, schemeful URL or it raises (caught, returns `False`). Since `message` here is the **raw message text**, not the extracted link, this only succeeds when the *entire* message is nothing but the bare URL. A message like `"check this out https://x.com/user/status/1"` throws inside `requests.get` and `fix_embed_if_social_link` silently returns `False` — v1 never rewrites a link that shares a message with any other text. This is an accident of validating the wrong string, not a deliberate anti-abuse check (there is no rate limit or content filter it protects), and it is also a synchronous, timeout-bound network call sitting directly on the hot reply path — exactly what AGENTS.md §2.4 forbids ("nothing slow on the reply path"). **Not preserved.** `cb_core.textmatch.find_embeddable_links` (already built and unit-tested, `packages/cb-core/tests/test_hot_modules.py::TestLinks`) finds a link anywhere in the text and finds several in one message; this port uses that contract instead of reproducing the bug. See "Deliberate fix" below.

### v1 defect #2 (not fixed — a real gap, flagged for the wiring owner) — TikTok short links are undetectable

`_EMBEDDABLE` in `cb_core/textmatch.py` (not owned by this port) matches `(?:www\.)?tiktok\.com/...` only — never the `vm.tiktok.com` subdomain v1's `TIKTOK_REGEX` also accepts for short links. A `vm.tiktok.com/ABC123` message is invisible to `find_embeddable_links`, so this handler can never attempt the redirect-and-rewrite v1 performs for it. Nothing in this port's file ownership can add that host to the shared regex; recorded here as a known parity gap, not silently dropped.

### Host list mismatch — `cb_core.textmatch._EMBEDDABLE` vs. v1's active hosts

v1 **actively** rewrites exactly three hosts: Twitter/X (-> `fixupx.com`), TikTok long-form (-> `vm.vxtiktok.com`), Bluesky (-> `fxbsky.app`). A fourth transformation for Instagram (-> `kkinstagram.com`) exists in the source but is commented out (`SocialContent.py:60,71-74`) together with its call site — **today's v1 does not rewrite Instagram links**, despite `INSTAGRAM_REGEX` still being defined at module level.

`cb_core.textmatch._EMBEDDABLE` (compiled, tested, not owned by this port) detects **nine** hosts as "embeddable": `x.com`, `twitter.com`, `bsky.app`, `instagram.com`, `tiktok.com`, `reddit.com`, `pixiv.net`, `e621.net`, `furaffinity.net`. Four of these — `reddit.com`, `pixiv.net`, `e621.net`, `furaffinity.net` — appear **nowhere** in `SocialContent.py`; v1 has never rewritten them, ever. Instagram is detected but, per above, currently dead in v1.

This is a real disagreement between the shared detector and v1's actual behaviour, flagged per this task's instructions rather than silently resolved by editing `textmatch.py` (out of this port's ownership). This port's resolution: use `find_embeddable_links` for *detection* only, and apply v1's three real host→target mappings for *rewriting*. A message with an Instagram, reddit, pixiv, e621 or furaffinity link produces no rewrite for that link — matching v1's actual behaviour for all five, not inventing a target domain nobody has verified against the real fixup services. If `util_embedder` is ever asked to actually support Instagram (matching v1's dead code) or the four hosts with no v1 precedent at all, that is new work for whoever owns `textmatch.py` plus this file, with real target domains confirmed first.

### Deliberate fix: link detection (not preserved from v1)

v1's "the whole message must be the bare link" restriction (defect #1 above) is not ported. `find_embeddable_links` finds a link anywhere in the text, and this handler rewrites **every** hit it can map to a known host, not just the first. Multiple links in one message produce one reply with each rewritten link on its own line, in the order they appeared. There is no real v1 behaviour to preserve here — v1's own multi-link handling was never reachable in practice, since any second link, or any surrounding text at all, already made `requests.get` throw before the transform loop ever ran.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/util_embedder.feature` verbatim into
`qa/features/util_embedder.feature` (both original scenarios, wording
unchanged), then added, for v1 behaviour the original spec never exercises:

1. **Twitter/X status link** and **TikTok video link** — the spec only names
   Bluesky as an example; v1 rewrites two other hosts the same way.
2. **Several embeddable links in one message** — the spec has no scenario for
   this at all; ties down the intentional multi-link behaviour above.
3. **Link already in embedded form** — pins v1's anti-double-rewrite guard
   (`SocialContent.py:51`), even though the mechanism differs (see "Deliberate
   fix" above: `find_embeddable_links` never detects a fixed-domain host to
   begin with, so the observable result is the same for a different reason).
4. **Instagram link** — pins that v1's *current*, not aspirational, behaviour
   is "no rewrite", since the source still contains a whole disabled
   transformation for it that a future change could accidentally re-enable
   without a test noticing.
5. **Link inside an ordinary sentence** — pins the deliberate divergence from
   defect #1: v1 would have silently done nothing here.
6. **Command containing a link** — pins that a command message never reaches
   this feature in v1, regardless of whether the command itself is recognised.
7. **Utility feature area disabled** — pins the one real precondition
   (`functionsUtility`/`ctx.enabled("utility")`) v1 has for this feature at all.

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/embedder.py`:

- `router.message(F.text, F.chat.type.in_({"group", "supergroup"}), FeatureGate("utility"))`
  — matches v1's private-chat exclusion and `functionsUtility` gate. `FeatureGate`
  is a genuine aiogram filter (not an in-handler check), so a disabled group
  never invokes this handler at all — the next router still gets the update,
  same as a v1 config check that simply skipped the call.
- `rewritten_links(text)` — pure function: `find_embeddable_links(text)` for
  detection, then `_rewrite_one(link)` per host (Twitter/X, TikTok long-form,
  Bluesky only) for the actual target, in the order links appear.
- `rewrite_embeddable_links(message)` — the handler. Raises `SkipHandler` for
  every path that does not act (command text, no `from_user`, no rewritable
  link), since this filter matches on every ordinary group text message with
  the utility area on and other handlers over the same update must still get
  their turn. Otherwise replies with the rewritten link(s) joined by `\n` and
  the same `LinkPreviewOptions` v1 sent.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Trigger scope: ordinary group text, not a command, not private | same | `F.text` + `F.chat.type.in_({"group","supergroup"})` + explicit `text.startswith("/")` check, raising `SkipHandler`. |
| Gated on `functionsUtility` / `ctx.enabled("utility")`, default on | same | `FeatureGate("utility")` filter; `functions_utility` defaults `True`, matching `Configurations.py:111`. |
| No admin check | same | no `AdminOnly` filter anywhere in this handler. |
| Requires a real sender (`'from' in msg`) | same | `message.from_user is None` -> `SkipHandler`. |
| Reply, never edit, never delete | same | `message.reply(...)`; no `edit_message_text`, no `delete_message` call anywhere in this file. |
| Reply body: just the rewritten URL(s) | same for a single link | multi-link case joins with `\n` — new shape, see below. |
| `link_preview_options` (`show_above_text`, `prefer_large_media`, not disabled) | same | ported byte-for-byte via `LinkPreviewOptions`. |
| Twitter/X -> `fixupx.com` | same | `_TWITTER_STATUS` regex + template, same target domain and path shape. |
| TikTok long-form -> `vm.vxtiktok.com` | same | `_TIKTOK_VIDEO` regex + template. |
| Bluesky -> `fxbsky.app` | same | `_BSKY_PROFILE` regex + template. |
| Instagram: not rewritten | same | v1's transformation is commented out; this port never rewrites it either (`test_instagram_is_detected_but_never_rewritten`). |
| Already-fixed-domain link: no re-rewrite | same (different mechanism) | `find_embeddable_links`'s host list never includes `fixupx.com`/`vxtwitter.com`/etc., so the link is never detected as embeddable to begin with — same observable result as v1's explicit skip-list. |
| "Validate" the message with a live `requests.get` before rewriting | **changed (intentional, fix)** | dropped entirely — see "v1 defect #1" above. Not just a style change: it removes a synchronous per-message network call from the reply path (AGENTS.md §2.4) and it was the mechanism silently breaking any link not sent alone. |
| Rewrite only a bare-URL message; silently ignore a link inside any other text | **changed (intentional, fix)** | dropped, a direct consequence of dropping defect #1. A link anywhere in a sentence is now rewritten (`test_link_embedded_in_a_sentence_is_still_found`, and the QA scenario "Link inside an ordinary sentence"). |
| Multiple embeddable links in one message | **changed (intentional, new capability)** | v1 never reached this case in practice (defect #1 made anything but a single bare link invisible); v2 rewrites every recognised link and joins them one per line. No v1 behaviour to contradict, since it was never reachable. |
| TikTok short link (`vm.tiktok.com/...`) redirect-and-rewrite | **known gap (not fixed, not owned)** | `cb_core.textmatch._EMBEDDABLE` cannot detect this host at all (see "v1 defect #2"); this port cannot add it without editing a file outside its ownership. |
| Host list disagreement: `reddit.com`, `pixiv.net`, `e621.net`, `furaffinity.net` detected by `find_embeddable_links` but never rewritten | **flagged, not fixed here** | see "Host list mismatch" above — `_rewrite_one` returns `None` for all four, matching v1's total absence of support, but the shared detector's broader host list is a real inconsistency worth resolving deliberately (add real targets, or narrow the regex) rather than by accident. |

## Known gaps for whoever owns the listed files

- `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not import or
  register `embedder.router` — needs `root.include_router(embedder.router)`
  (plus the import) for `qa/test_util_embedder.py`'s five positive-reply
  scenarios to pass end to end (the five negative scenarios already pass,
  trivially, since no handler runs at all yet). Verified independently outside
  the shared dispatcher: wiring `embedder.router` alone into a throwaway
  `Dispatcher` and feeding it a `https://x.com/someuser/status/123` message
  produces exactly `sendMessage(text="https://fixupx.com/someuser/status/123",
  link_preview_options={"show_above_text": true, "prefer_large_media": true,
  "is_disabled": false}, reply_parameters=...)` — the handler itself is
  correct; only the registration is missing. Out of this port's file
  ownership.
- `cb_core/textmatch.py`'s `_EMBEDDABLE` host list disagrees with v1 in two
  ways: it is missing the `vm.tiktok.com` short-link subdomain v1 supports,
  and it includes four hosts (`reddit.com`, `pixiv.net`, `e621.net`,
  `furaffinity.net`) v1 has never rewritten at all, plus `instagram.com`,
  which v1's source defines but has disabled. Not this port's file to edit;
  recorded here per the task's instruction to report the disagreement rather
  than silently pick a side.
- `docs/FEATURE-MAP.md`'s `util_embedder` row could use a note pointing at the
  host-list mismatch above; this agent could not edit that file.
