# fun_battle — Design

Both open decisions from `spec.md` are settled:

1. **Redesign accepted.** Resolve targets through `cb_core.members.roster`,
   fetch photos through `bot.get_user_profile_photos` (the same Bot API call
   v1's own path C already used), hand the returned `file_id` straight into
   a new `InputMediaPhoto` — no download, no OpenCV, no temp files. D-BT-1
   (the temp-file race) and D-BT-2 (the `telegram.me` scrape) both disappear
   with the code that caused them, not by exclusion.
2. **Ship path A now.** Two-tagged/`"random"` battles go live in this slice.
   Paths B (one tag) and C (no tag) reply `battle_no_picture` — reused
   verbatim, not a new string — and stop, until the `Fight/` bucket export
   (`fun_death`'s same prerequisite) lands. This is explicitly a temporary
   route into an existing v1 failure branch, not the final behaviour for
   B/C — every task and the contract say so.

## Module placement

| Piece | Where | Reuses |
|---|---|---|
| Handler | `packages/cb-gateway/src/cb_gateway/handlers/battle.py` (new) | `cb_core.members.roster`, `cb_gateway.context.context_for`/`t`, `ctx.enabled("fun")`, `CommandName("battle")` |
| Nested/mistyped-catalog readers | local in `battle.py` | copies `groupguardian.py:108-125`'s `_captcha_strings` cast pattern for `battle_type`/`battle_rule`/`battle_equip` (top-level **list** values — `locales.get` is typed `-> str` and would silently hand back the wrong shape if called on these directly) |
| Router registration | `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` | one line, disjoint trigger |

No database, no migration, no worker job, no new dependency. Everything —
including path A's two Bot-API calls per battle — is fast enough for the
reply path; nothing here is the "external HTTP scrape + image decode" shape
AGENTS.md §2.4 would push into `cb-worker`, because that shape no longer
exists once the redesign lands.

## R1 — target parsing (pure, ported byte-for-byte)

**R1.1** `parse_tagged_targets(text: str) -> list[str]` replicates
`get_members_tagged` (`SocialContent.py:104-111`) exactly: split on `"@"`,
drop the head (`[1:]`), drop anything ending in the literal lowercase
substring `"bot"` — **case-sensitive**, matching v1's `.endswith('bot')`, so
`"@AdminBot"` is *not* filtered (ends in `"Bot"`, not `"bot"`) while
`"@spambot"` is. The raw split means a target can carry trailing text up to
the next `@` or end of string — `"/battle @alice fight @bob"` yields
`["alice fight ", "bob"]`, not `["alice", "bob"]` — a genuine v1 quirk
(untrimmed, sometimes multi-word). **Preserved for display** (R4 below);
**not** used raw for resolution (R2).

**R1.2** `_leading_token(raw: str) -> str` — `raw.strip().split()[0] if
raw.strip().split() else ""`. Used only to turn a raw tagged capture into
something that could plausibly be a username, for the roster lookup. Never
shown to a user; R1.1's raw string is what appears in captions/choices.

**R1.3** Shape selection, mirroring v1's `if len(members_tagged) > 1 or
'random' in msg['text'].lower(): ... elif len(members_tagged): ... else:`
exactly (`SocialContent.py:298,345,358`):

```python
class BattleShape(enum.Enum):
    TWO_PEOPLE = enum.auto()  # path A: 2+ tags, or "random" anywhere in the text
    ONE_TAG = enum.auto()  # path B: exactly 1 tag
    SELF = enum.auto()  # path C: no tag, no "random"
```

`"random"` is checked against the *whole* message text, lowercased, same as
v1 (`'random' in msg['text'].lower()`) — not just the args, so a group whose
custom banter happens to contain the word "random" after `/battle` also
takes this branch, exactly as v1 would.

## R2 — resolving TWO_PEOPLE to real people (R1's accepted redesign)

**R2.0 — `"random"` wins over explicit tags.** v1's inner check
(`SocialContent.py:298-299`) is `if len(members_tagged) > 1 or 'random' in
text: if 'random' in text: <random path> else: <two-tags path>` — the
`"random"` sub-branch is checked *again*, unconditionally, once shape
selection has already decided `TWO_PEOPLE`. A message with **both** two
`@tags` and the word `"random"` anywhere takes the random-pick path, not
the explicit tags — preserved exactly, not simplified to "tags win when
present."

**R2.1** Otherwise (2+ explicit tags, `"random"` absent): only the first
two are ever used, matching v1's `members_tagged[0]`/`[1]` — a third+ tag
is silently ignored. Resolve each via `_leading_token` against
`cb_core.members.roster(group_id)`, case-insensitively (Telegram usernames
are canonically case-insensitive; v1 got this for free by feeding the raw
string to `telegram.me`, which resolves usernames case-insensitively
server-side — matching that outcome here, not v1's mechanism, is the same
"fix the mechanism, preserve the result" call already made for the scrape
itself).

**R2.2** `"random"` (R2.0): sample 2 distinct roster entries whose
`username` is not `None` (v1's `'user' in members[0]` requirement,
`SocialContent.py:307`) via `rng.sample(candidates, 2)`. Fewer than 2
eligible candidates ⇒ `battle_no` (v1's exact check, `:301-304`) — this
replaces v1's up-to-100-retries reshuffle loop with a single `sample` call
over a pre-filtered candidate list, which cannot fail to find 2 when 2
exist and cannot loop when they do not; not a behavioural difference a user
could observe, only a cleaner way to reach the same result.

**R2.3** For each of the two resolved sides, in order (first side's failure
reported before the second's is even attempted — v1's exact order,
`:315-322`): `bot.get_user_profile_photos(member.user_id, limit=1)`. Empty
`photos` ⇒ `battle_extract` naming that side — **using the actual resolved
display token** (R1.1's raw capture for a tag, or the picked username for
`"random"`), never an internal id. This is also where D-BT-3 (a newly
found v1 crash: `battle_extract`'s `%(user)s` substitution used
`members_tagged[0]`, which is out of range whenever the `"random"` branch
was reached with zero explicit `@` tags in the message — `/battle random`
verbatim, extraction failing on the first pick) disappears as a natural
consequence of always naming the side that actually failed, rather than
indexing into a list that may not correspond to which branch is live.

**R2.4** A tag that does not match anyone in the roster (never spoken in
this group — the accepted behavioural drift from `spec.md`) is treated
identically to "resolved but no profile photo": `battle_extract`, naming
the raw tagged token. This is deliberately v1's **existing** failure
message, reached a new way, not a new failure path — record this in
`docs/contracts/fun_battle.md` as the drift the redesign costs, exactly as
directed.

**R2.5** Both sides resolved with a photo: `file_id = photos.photos[0][-1].file_id`
(largest available size of the most recent photo — v1's exact indexing,
`['photos'][0][-1]['file_id']`) for each side, no download.

## R3 — ONE_TAG / SELF: the temporary route (accepted scope cut)

**R3.1** Both shapes reply `t(ctx, "battle_no_picture")` and return —
**no roster lookup, no Bot API call, no photo resolution attempted at
all**, since the eventual output needs a `Fight/`-bucket image regardless
of whether the human side would have resolved. Detecting the shape (R1.3)
is the only work done before replying.

**R3.2** This is explicitly temporary. A code comment at the branch site
(not just this doc) says so, points at `fun_death`'s contract for the
shared prerequisite, and names what changes once `Fight/` is vendored:
paths B/C stop being a flat reply and gain the same photo-resolution +
fighter-image + poll flow path A already has, with the fighter side sourced
from `cb_core.assets` the way `fun_death`'s (still-blocked) design already
specifies for its own pool.

## R4 — caption, poll, and the `@`-prefix quirk (preserved byte-for-byte)

**R4.1** Flavour suffix — `battle_full` with three random picks
(`battle_type`/`battle_rule`/`battle_equip`, all top-level catalog
**lists**): a local `_catalog_choice(lang, key, rng)` helper does the same
cast-and-en-fallback `groupguardian.py`'s `_captcha_strings` established,
then `rng.choice(...)`. `poll_title = t(ctx, "battle_title")` — always this
key for path A; `battle_title_plus`/`battle_title_list` (PT-only, no `en`/
`es` entries) belong to B/C's fighter tail and are out of scope here.

**R4.2** Caption and poll choices, exactly v1's two shapes
(`SocialContent.py:328-333`) — **the `@`-prefix inconsistency between them
is preserved, not normalised**, same category as `fun_ship`'s
`@@alice + @@bob` double-sigil (already a house precedent for "a cosmetic
quirk visible to users who have typed this command for years"):

- Two explicit tags: `caption = f"{raw_tag_a} VS {raw_tag_b}"` — **no** `@`
  (the raw captures from R1.1 never carried one; v1's own caption doesn't
  add one back). `choices = [raw_tag_a, raw_tag_b]`.
- `"random"`: `caption = f"@{username_a} VS @{username_b}"` — **with** `@`.
  `choices = [username_a, username_b]` — no `@` in the poll choices either
  way, only the caption differs between the two sub-shapes.

**R4.3** `medias[0]`'s caption carries the full string (flavour suffix
appended); `medias[1]` has none — v1's exact shape (`caption` is set on
`medias[0]` only, `:341`/`:377`).

**R4.4** Poll: `is_anonymous=False`, `allows_multiple_answers=False`,
options built as `[InputPollOption(text=choices[0]), InputPollOption(text=choices[1])]`
(aiogram's typed shape for a bare string option — v1's `sendPoll` took bare
strings under an older Bot API; the observable poll a group sees is
unchanged, this is purely aiogram's typed-API requirement). Both the media
group and the poll are **replies** to the trigger message (v1's
`reply_to_message_id=msg['message_id']` on both calls, `:342-343`).

## R5 — telemetry

**R5.1** `mark_outcome("refused")` on the `fun_off` gate only — same
convention `fun_ship`'s `no_ship` fallback already set: a reply that
explains why the feature didn't fully run (`battle_no`, `battle_extract`,
`battle_no_picture`) is a real answer, not a refusal, and gets no explicit
`mark_outcome` call (defaults to `"answered"`).

## R6 — reaction and chat action

**R6.1** `message.react(reaction=[ReactionTypeEmoji(emoji="🔥")])`,
best-effort suppressed (same `contextlib.suppress(Exception)` idiom every
other handler in this codebase uses for a reaction) — **before** any
branching, matching v1's `react_to_message` call being the very first line
of `battle()` (`:295`), so it fires even on `battle_no`/`battle_extract`/
`battle_no_picture`.

**R6.2** `bot.send_chat_action(chat_id, "upload_photo")` — v1 sends this
once, immediately after the reaction, regardless of path (`:296`); ported
the same way.

## Open decisions — answered

1. Redesign accepted (see top of this document).
2. Ship path A now; B/C reuse `battle_no_picture` until unblocked (see top
   of this document).
3. **Roster lookup is case-insensitive.** Settled in R2.1 — matches the
   outcome v1's own mechanism produced, not a new capability.
4. **`"random"` picks by `rng.sample`, not a retry loop.** Settled in
   R2.2 — behaviourally identical, structurally simpler.
