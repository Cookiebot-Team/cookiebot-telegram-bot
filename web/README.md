# Cookiebot sandbox — web client

The UAT workbench for the Cookiebot v2 Telegram bot: a Telegram-Desktop-shaped
UI, backed by `packages/cb-sandbox` (a fake Bot API + a control plane), that
drives the real, unmodified bot stack. See `docs/SANDBOX.md` for what this is
for and how to wire the three processes together; this file covers the parts
that are specific to the `web/` app itself.

## Running it

```bash
bun install
bun run dev      # :3001, proxies /api/* to the sandbox server (next.config.ts)
```

`bunx tsc --noEmit` and `bun run build` are the two gates. **bun only** —
`bun.lock` is the lockfile; do not run npm/yarn/pnpm here, and do not let
`package-lock.json` come back.

## Shape

```
app/page.tsx                 the three-pane shell; owns acting-user/chat/reply
                              state and the "did the bot answer yet" indicator
components/chat/              chat list, message bubbles, composer, inline keyboard
components/chat/MessageMedia  photos, stickers, video, audio and documents, rendered
                              from the real bytes the sandbox stores
components/sandbox/           the feature rail, the scenario rail, user switcher,
                              membership controls, seed presets, command palette,
                              the API-call log
lib/api.ts                    the ONE typed client for cb-sandbox's control API
lib/lens.ts                   the ONE filter predicate — feature + scenario — that
                              every pane goes through, so no two panes can disagree
                              about what is currently being shown
lib/format.ts                 the ONE set of formatting/colour helpers
lib/commands.ts               grouping and search over the palette served by /api/kit
lib/useSandbox.ts             snapshot + kit + live state (SSE, with a polling fallback)
lib/sanitizeHtml.tsx          renders the bot's <b>/<i>/<code>/<blockquote> HTML
types.ts                      shapes shared with cb_sandbox/control_api.py
```

`components/sandbox/ScenarioPanel.tsx` and `components/sandbox/ScenarioRail.tsx`
sound like the same idea and are not. `ScenarioPanel` picks a **seed** (a fixed
starting world: users, a group, who is a member — `SandboxSeed` in `types.ts`,
served by `/api/kit`). `ScenarioRail` is the **scenario lens**: the server's
`Scenario`, a named span of time layered on top of whatever world is already
seeded, that every message and API call gets tagged with while it is the active
one. The sidebar's section headers say "Seed data" and "Scenario" for exactly
this reason — see `ScenarioRail.tsx`'s own header comment for the full story.

There used to be a second, divergent copy of `lib/api.ts` and `lib/format.ts`
under `components/sandbox/` (`api.ts`, `format.ts`), written by an agent that
didn't know the canonical ones existed, guessing at request shapes the real
`control_api.py` didn't match (`from_id` where the server wants `user_id`,
`type` on chat creation the server never accepts). They're gone; `lib/` is the
only copy of either now, and every component in `components/sandbox/` imports
from there.

## The contract with cb-sandbox

`web/types.ts` mirrors `packages/cb-sandbox/src/cb_sandbox/control_api.py`'s
pydantic models field-for-field, and `lib/api.ts`'s request shapes are
transcribed from that file's `*Request` models, not guessed — the previous
version of this client was written before `control_api.py` existed and every
send 422'd as a result. If you add or change a control-API route:

1. Change `control_api.py` and add/extend a test in
   `packages/cb-sandbox/tests/test_control_api.py`.
2. Update `web/types.ts` to match the new/changed pydantic model.
3. Update the matching function in `web/lib/api.ts`.

`control_api.py` and its test file are owned by whoever owns `web/` (the same
person/session), specifically so this loop doesn't cross an ownership
boundary. `state.py`, `telegram_api.py` and `persistence.py` are not — treat
those as read-only from here.

One field is deliberately *not* narrowed on the client even though the rest
of this file mirrors `control_api.py` exactly: `Scenario.status` is typed
`string`, matching `SandboxScenario.status: str` server-side, not a closed
union — see that dataclass's own docstring for why (short version: the e2e
suite and manual testers get to invent their own vocabulary, and nothing
gates behaviour on the value). Don't "fix" this into a `Literal` without
checking `state.py` first.

## Nothing in here knows it is Cookiebot

The bot's identity, its seed worlds, its features, its presets and its whole
command palette arrive at runtime from `GET /api/kit`, which the server builds
from `sandbox.config.json`. There is no generated JSON in this directory and no
Cookiebot-specific array in any component — swap the config file, restart the
sandbox, and this same client drives a different bot.

That config is generated from the two places that already know the truth:

```bash
python scripts/cb.py sandbox-config
```

It reads `cb_core.textmatch.COMMAND_ALIASES` (every trigger word the parser
accepts, canonicalised) and `scripts/spec.py` (each feature's port status), so
the palette always reflects what the bot actually recognises and whether it is
wired up yet — a "planned" command shows as "not implemented — expect silence"
rather than reading as a bug when it does not reply. Regenerate it whenever
`COMMAND_ALIASES` gains an alias or a feature's status moves.

## Two filters, one predicate

The sidebar filters on two axes and they answer different questions:

- **Feature** — "is this behaviour correct?", across every scenario that
  touched it. This is how validation actually happens: one behaviour at a time,
  not one test at a time. `FeatureRail.tsx`, sorted so failures and *untested*
  features come first.
- **Scenario** — "what did *this* check do?", drilled into one span.

Both live in a single `Lens` object in `lib/lens.ts`, and every pane — the
timeline, the API-call log, the scenario picker — filters through the same
`matchesLens`. That is not tidiness: two panes silently applying different
filters is a validation tool that lies.

## Notable UI decisions

- **One acting-user selection.** `app/page.tsx` owns `currentUserId`
  (persisted to `localStorage`) and passes it down everywhere. An earlier
  version had two independent copies — one in `ChatList`'s dropdown (real,
  wired to sends) and one inside `SandboxSidebar`/`UserSwitcher` (its own
  `useState` + `localStorage`, never connected back up) — so picking a user in
  the sidebar silently did nothing to who the composer sent as. There is
  exactly one now.
- **Anonymous admin is loud, not a footnote.** A user chip and a member row
  both get a gold ring/badge the moment `Membership.anonymous` is true for
  the open chat, and the composer only shows the "post anonymously" toggle
  when it would actually be honoured by the server. This is the case v1 got
  wrong (an anonymous admin was rejected and told to disable a Telegram
  feature) and the single most valuable thing this sandbox can reproduce.
- **Repeat is cheap two ways.** The composer's `×N` field sends the same
  message that many times in one click (sticker-spam moderation is only
  exercisable past a flood limit); `Alt+.` resends the last thing sent from
  anywhere, without retyping it.
- **Silence is distinguishable from loading.** The activity line under the
  chat header ticks through three states — waiting (just acted), silent (a
  couple of seconds with nothing back — a valid outcome), answered (with the
  measured latency) — because "the bot didn't reply" and "the request is
  still in flight" are different results and this tool exists so a tester
  doesn't have to guess which one they're looking at.
- **The API-call log is the validation surface.** Filterable by method,
  each row expandable to the full JSON payload with a copy-to-clipboard
  button, and every call invisible to a chat window (`deleteMessage`,
  `restrictChatMember`, `banChatMember`, `answerCallbackQuery`) gets an
  explicit marker. Timestamps read relative to the tester's last action
  (`+340ms`) while that action is still the freshest thing that happened, so
  "I sent this, then the bot did those three things" doesn't need
  cross-referencing two clocks.
- **`useSandbox` polls underneath its SSE stream.** `control_api.py`'s
  `stream_events` only fires for call sites that already publish an event
  (a send, a membership change); several Bot API calls the log needs to show
  (`answerCallbackQuery`, `getChatAdministrators`, ...) have nothing to
  publish. A ~1.2s poll is the backstop that keeps the log and the activity
  indicator honest regardless.
- **The scenario filter is loud on purpose.** After an e2e run or an
  afternoon of manual UAT the sandbox holds traffic from dozens of separate
  checks with nothing distinguishing one from another. `ScenarioRail.tsx`'s
  picker filters the message timeline and the API-call log to one scenario at
  a time ("untagged" is its own selectable bucket, not a hidden default), and
  whenever a filter is active it gets an amber banner above the chat header —
  not folded into the existing "Testing: …" preset banner — because a tester
  who forgets a filter is on and concludes "the bot did nothing" is the
  exact failure this feature must not introduce. When nothing is filtered,
  every message bubble and API-call row instead gets a small badge naming the
  scenario that produced it, so cross-scenario noise in an unfiltered view is
  still legible.
- **The scenario's own status is a free-form string, not a client-side
  enum.** `SandboxScenario.status` on the server is a plain `str` — the e2e
  suite and a human doing manual UAT are free to write whatever word tells
  the next reader what happened, and `control_api.py` never gates behaviour
  on it. `ScenarioRail.tsx` gives `"running"`/`"passed"`/`"failed"` (its own
  pass/fail controls' vocabulary, plus the server's own two defaults) a
  distinct colour each; anything else still renders, as a plain pill showing
  the exact word rather than swallowing it as "unknown".

## Keyboard shortcuts

All global, all `Alt+…`, all wired in `app/page.tsx`'s one `keydown` listener
so there's a single place that knows the full list:

| Shortcut  | Effect |
|-----------|--------|
| `Alt+1`–`9` | Switch acting user (matches the index on each chip's tooltip in `UserSwitcher`). |
| `Alt+0`   | Clear the scenario filter back to "All scenarios". |
| `Alt+.`   | Resend the last thing sent, from anywhere. |
| `Alt+R`   | Reset the sandbox (same confirmation as the sidebar button). |
| `Alt+N`   | Focus the note field for the active scenario (`ScenarioRail.tsx`) — a shortcut can't supply the note text itself, so this gets you to where you type it, not a submitted blank note. |
| `Alt+P`   | Mark the active scenario passed. |
| `Alt+F`   | Mark the active scenario failed. |

`Alt+N`/`Alt+P`/`Alt+F` always act on `active_scenario_id` — the scenario
currently tagging new traffic — never on whatever the scenario filter happens
to be displaying, so a keystroke fired mid-flow can't land a note or a
verdict on the wrong scenario.

## No new runtime dependencies

Everything here is Next.js/React/Tailwind plus hand-rolled helpers
(`lib/format.ts`, `lib/sanitizeHtml.tsx`). Formatting, colour palettes and
HTML rendering are all small enough that a library would cost more in
bundle size and review surface than it would save — see each file's own
docstring for why it doesn't reach for one.
