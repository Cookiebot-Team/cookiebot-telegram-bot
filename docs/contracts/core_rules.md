# Contract: core_rules (v1 -> v2)

Phase 2/6 of `/migrate-feature` for `/rules` and `/newrules`. QA:
`../Cookiebot-QA/features/core_rules.feature`. FEATURE-MAP row: `core_rules`,
status `✅`. Files owned by this port: `packages/cb-gateway/src/cb_gateway/handlers/rules.py`,
`qa/features/core_rules.feature`, `qa/test_core_rules.py`,
`packages/cb-gateway/tests/test_rules.py`, `qa/integration/test_group_rules.py`,
this file.

## Phase 2 — v1 behaviour contract

v1 handlers:

```python
# GroupShield.py:49-63 — display
def rules_message(cookiebot, msg, chat_id, language):
    send_chat_action(cookiebot, chat_id, "typing")
    rules = get_request_backend(f"rules/{chat_id}")
    if (type(rules) is str and not len(rules)) or (
        "error" in rules and rules["error"] == "Not Found"
    ):
        text = i18n.get("no_rules", lang=language)
        send_message(cookiebot, chat_id, text, msg, language)
    else:
        regras = rules["rules"].replace("\\n", "\n")
        regras = substitute_user_tags(regras, msg)
        if not len(regras):
            return
        if not regras.endswith("@MekhyW"):
            additional = i18n.get("questions", lang=language)
            regras += additional
        cookiebot.sendMessage(chat_id, regras, reply_to_message_id=msg["message_id"])


# Configurations.py:281-283 — prompt
def new_rules_message(cookiebot, msg, chat_id):
    send_chat_action(cookiebot, chat_id, "typing")
    cookiebot.sendMessage(
        chat_id,
        "If you are an admin, REPLY THIS MESSAGE with the message "
        "that will be displayed when someone asks for the rules",
        reply_to_message_id=msg["message_id"],
    )


# Configurations.py:269-279 — capture
def update_rules_message(cookiebot, msg, chat_id, listaadmins_id, is_alternate_bot=0):
    if (
        str(msg["from"]["id"]) not in listaadmins_id
        and "sender_chat" not in msg
        and int(msg["from"]["id"]) != ownerID
    ):
        send_message(cookiebot, chat_id, "You are not a group admin!", msg_to_reply=msg)
        return
    send_chat_action(cookiebot, chat_id, "typing")
    req = put_request_backend(f"rules/{chat_id}", {"rules": msg["text"]})
    if "error" in req and req["error"] == "Not Found":
        post_request_backend(f"rules/{chat_id}", {"rules": msg["text"]})
    react_to_message(msg, "👍", is_alternate_bot=is_alternate_bot)
    cookiebot.sendMessage(
        chat_id, "Updated rules message! ✅", reply_to_message_id=msg["message_id"]
    )
    delete_message(cookiebot, telepot.message_identifier(msg["reply_to_message"]))
```

Dispatched from `COOKIEBOT.py`:

```python
# :186 — the whole command-dispatch chain lives inside this if
if msg['text'].startswith("/") and len(msg['text']) > 1:
    ...
    elif msg['text'].startswith(("/novasregras", "/newrules", "/nuevasreglas")):   # :266
        new_rules_message(cookiebot, msg, chat_id)
    elif msg['text'].startswith(("/regras", "/rules", "/reglas")):                # :268
        rules_message(cookiebot, msg, chat_id, language)
    ...
# :293 — sibling elif of the `if` at :186, so only reached when the text does
# NOT start with "/" (or is a lone "/")
elif 'reply_to_message' in msg and 'text' in msg['reply_to_message'] and \
        msg['reply_to_message']['text'] == "If you are an admin, REPLY THIS MESSAGE with the "
                                            "message that will be displayed when someone asks for the rules":
    listaadmins, listaadmins_id, _ = get_admins(cookiebot, chat_id, ignorecache=True, is_alternate_bot=is_alternate_bot)
    update_rules_message(cookiebot, msg, chat_id, listaadmins_id, is_alternate_bot=is_alternate_bot)
```

| Aspect | v1 behaviour (file:line) |
|---|---|
| Triggers (`/rules`) | `msg['text'].startswith(("/regras", "/rules", "/reglas"))`, `COOKIEBOT.py:268`. Already in `cb_core/textmatch.py:COMMAND_ALIASES` (`rules`,`regras`→`rules`). |
| Triggers (`/newrules`) | `msg['text'].startswith(("/novasregras", "/newrules", "/nuevasreglas"))`, `COOKIEBOT.py:266`. Already in `COMMAND_ALIASES` (`newrules`,`novasregras`→`newrules`). Spanish `nuevasreglas` maps to `newrules` via the same key already present. |
| Preconditions (`/rules`) | None. No admin check, no feature-flag gate — sits before any `functionsFun`/`functionsUtility` check, same as `/privacy`. |
| Preconditions (`/newrules`) | **None on the command itself.** v1 never checks who ran `/newrules` — the prompt is shown to anyone. Admin status is only checked later, when someone *replies* to that exact prompt text (`Configurations.py:270`), and the whole reply-capture branch is only reachable when the reply's own text does not itself start with `/` (`COOKIEBOT.py:186,293` — see the "elif" note above). |
| How the new text is captured | **A two-step conversation, not an argument.** `/newrules` sends a fixed, hardcoded prompt (`Configurations.py:283`). Whoever replies to that literal prompt text (byte-for-byte comparison, `COOKIEBOT.py:293`) has their reply's own text used as the new rules body (`Configurations.py:274`, `msg['text']`). v1 does not check that the replier is replying to the *bot's own* message — only that the replied-to text matches, and does not check that the reply itself has a `text` key (a non-text reply would `KeyError` inside `put_request_backend`; v2 fixes this — see Phase 6). |
| Who may set rules | Whoever replies to the prompt, if `str(msg['from']['id']) in listaadmins_id` **or** `'sender_chat' in msg` (anonymous admin — Telegram only lets a message carry `sender_chat` when the sender already holds admin rights, see `docs/contracts/admins.md`) **or** the hardcoded bot owner. This is the accidentally-correct anonymous-admin pattern (`admins.md`'s "six other call sites" list includes this one). |
| Failure output (non-admin reply) | Hardcoded, **not localised** (unlike almost every other v1 string): `"You are not a group admin!"` (`Configurations.py:271`), sent as a reply to the rejected message. |
| Success output (`/newrules` prompt) | Hardcoded, not localised: `"If you are an admin, REPLY THIS MESSAGE with the message that will be displayed when someone asks for the rules"` (`Configurations.py:283`), sent as a reply to the `/newrules` message. |
| Success output (rules saved) | Hardcoded, not localised: `"Updated rules message! ✅"` (`Configurations.py:278`), reply to the admin's submission. Side effects: a 👍 reaction on the submission (`react_to_message`, `universal_funcs.py:300-305`, best-effort HTTP call — **not ported**, see Phase 6) and deletion of the original prompt message (`delete_message`, `universal_funcs.py:340-344`, swallows its own errors). |
| Empty state (`/rules`, no row) | `i18n.get("no_rules", lang=language)` (`GroupShield.py:53`) — catalog key `no_rules`, already ported byte-for-byte to `cb_core/locale_data/{en,pt,es}/lib.json`. English: `"There are no rules set for this group yet\n<blockquote> If you are an admin and want to set rules, use /newrules </blockquote>"`. **Not** the paraphrase in the upstream QA spec (see Phase 6). |
| Success output (`/rules`, row exists) | The stored body, with literal `\n` escapes unescaped (`.replace('\\n', '\n')`), then `substitute_user_tags` applied (`GroupShield.py:38-47`, replacing any of ten tag spellings with the *requester's* own `@username` or first name), then — unless the resulting text already ends with the literal string `"@MekhyW"` — the catalog key `questions` is appended (`"\n\nQuestions about the bot? Send to @MekhyW"` in English). If the result is empty after substitution, v1 sends **nothing at all** (`GroupShield.py:58-59`), distinct from the empty-state message. |
| Persistence | `group_rules` table (`packages/cb-api/migrations/versions/0001_initial_schema.py:206-214`): `group_id` (PK, FK -> `groups` `ON DELETE CASCADE`), `body`, `updated_by`, `updated_at`. v1's REST layer did PUT-then-POST-on-404 against a Mongo-backed `rules/{chat_id}` resource; the v2 equivalent is a single upsert. |
| Side effects | `send_chat_action(..., 'typing')` before both the display and the prompt (cosmetic, not ported — same call already dropped for `/privacy`, see its contract). |
| External calls | None beyond the (v1) backend REST call this port replaces with a direct DB round trip. |
| Known defects | None of FEATURE-MAP's D1-D13 apply directly (no shared unbounded cache here). The QA spec mismatch below is new to this feature. |

### QA-spec / v1 mismatches found while writing this contract

1. **The "not an admin" scenario for `/newrules`** (`core_rules.feature`, upstream) asserts the message `"You don't have permission to use this command or are in anonymous mode"` plus a tutorial video, triggered by the bare act of running `/newrules` as a non-admin. That text and video are v1's `/configurar` behaviour (`Configurations.py:141-144`, mirrored verbatim in `util_config.feature` and `core_welcome.feature`) — **not** what `/newrules` does. v1 never gates the `/newrules` command on admin status at all (see the table above); the real rejection only happens on the reply, with the different, hardcoded text `"You are not a group admin!"`. Per AGENTS.md ("v1 code wins for observable behaviour, QA wins for intent"), the port follows v1's real behaviour. The scenario's Given/When/Then wording is kept 100% verbatim in `qa/features/core_rules.feature`; the step definitions in `qa/test_core_rules.py` (`bot_says_on_group`, `bot_displays_video`) special-case this exact copied string and assert the real, observable output (the `/newrules` prompt, no video) instead of retyping the mismatched quote. (An actual `@xfail` tag was tried first but had to be dropped — this repo's pytest-bdd/gherkin/Python 3.14 combination raises a `DeprecationWarning`-as-error from inside the `gherkin` parser library on *any* `@tag` line during collection, unrelated to this feature.) The real non-admin rejection is covered by a new scenario ("A user who is not an admin replies to the /newrules prompt"). **This needs a FEATURE-MAP.md note**, which this agent could not add (file not owned by this task) — flagged for whoever owns it.
2. **The "no rules set" scenario's quoted text** (`"No rules have been set for this group yet. Please contact an admin to set the rules using /newrules command"`) is a paraphrase of v1's real `no_rules` catalog string, not a verbatim quote. The port answers with the ported catalog string (byte-identical to v1's `Bot/Static/locales/eng/lib.json`), and `qa/test_core_rules.py` asserts against the catalog value directly rather than retyping either string.

## Phase 3 — QA scenario

Copied `../Cookiebot-QA/features/core_rules.feature` verbatim into
`qa/features/core_rules.feature` (including the mismatched scenario above,
wording unchanged — see mismatch #1 for how its step definitions handle the
mismatch), then added: the reply-capture mechanics for both the
admin and non-admin path (the original only asserts the admin path in prose
with no matching scenario), the anonymous-admin case, the PT/ES aliases for
both commands, and a command addressed at a different bot being ignored
(mirrors the pattern already established in `core_privacy.feature`). One line
("Given the group already has rules configured") was added to the existing
"view rules" scenario and its new PT/ES-alias siblings, since the two original
`/rules` scenarios are otherwise textually identical and only distinguishable
by scenario title — upstream leaves the precondition implicit.

## Implementation (v2)

`packages/cb-gateway/src/cb_gateway/handlers/rules.py`:

- `router.message(CommandName("rules"))` / `router.message(CommandName("newrules"))` —
  no `FeatureGate`, no `AdminOnly` on either, matching v1's unconditional dispatch.
- `_is_new_rules_reply(message)` — a plain filter function (not `AdminOnly`)
  replicating v1's structural precondition: reply's own text must not itself
  look like a command, and must reply to the exact prompt text.
- Admin check lives *inside* `capture_new_rules`, via `context_for(...).is_admin`
  (`cb_gateway.context`, which already resolves anonymous admins correctly —
  `docs/contracts/admins.md`), not `AdminOnly()` as a filter — a filter would
  make the bot silently drop a non-admin's reply instead of telling them they
  are not an admin, which is not what v1 does.
- `_fetch_rules` / `_upsert_rules` — the DB seam this handler owns, single-shard
  reads/writes filtered on `group_id`, upserting `group_rules`.

## Phase 6 — Parity table

| Row | Verdict | Note |
|---|---|---|
| Triggers `/rules`,`/regras`,`/reglas` | same | via existing `COMMAND_ALIASES` + `CommandName("rules")`. |
| Triggers `/newrules`,`/novasregras`,`/nuevasreglas` | same | via existing `COMMAND_ALIASES` + `CommandName("newrules")`. |
| `/rules@OtherBot` ignored | changed (intentional, fix) | same fix already applied to `/privacy`; v1's naive `startswith` would answer regardless of target. |
| `/newrules` never gated on admin | same | no `AdminOnly()` filter on the command; prompt shown to anyone. |
| New-rules text capture: two-step reply, not an argument | same | `_is_new_rules_reply` matches on `reply_to_message.text == NEW_RULES_PROMPT`. |
| Reply-capture only reached when the reply's own text isn't itself a command | same | ported explicitly (`COOKIEBOT.py:186,293`); see `TestIsNewRulesReply` in `packages/cb-gateway/tests/test_rules.py`. |
| Reply must specifically be to *the bot's* prompt message | changed (intentional, kept as-is) | v1 doesn't check this either (only text equality) — same laxity preserved; the admin check on the write path is what actually gates a bad write, matching v1's real safety net. |
| Non-text reply to the prompt | changed (intentional, fix) | v1 would `KeyError` inside `put_request_backend` on `msg['text']` for a photo/sticker reply (uncaught, falls into the owner-mailing top-level handler — total silence for the group). v2's `_is_new_rules_reply` requires `message.text is not None`, so a non-text reply is simply not captured — no crash, no silent drop of an unrelated update either. |
| Admin-only write, anonymous admin included | same | `ctx.is_admin` via `context_for` (`docs/contracts/admins.md`); v1 got this right by accident via `'sender_chat' not in msg`. |
| Non-admin rejection text `"You are not a group admin!"` | same | hardcoded, unlocalised, exactly as v1 (`Configurations.py:271`) — a preserved quirk, not fixed, since it is user-visible but harmless. |
| Prompt text | same | hardcoded, unlocalised, byte-identical (`Configurations.py:283`). |
| Confirmation text `"Updated rules message! ✅"` | same | hardcoded, unlocalised, byte-identical (`Configurations.py:278`). |
| Deleting the prompt after a successful submission | same | `message.bot.delete_message(...)` wrapped in `contextlib.suppress(Exception)`, matching v1's swallow-and-print (`universal_funcs.py:340-344`). |
| 👍 reaction on the admin's submission | not ported | cosmetic-only fire-and-forget call (`react_to_message`, `universal_funcs.py:300-305`); no other ported handler sends a reaction and it is not observable in the QA scenario. Flagged here rather than silently dropped. |
| `send_chat_action('typing')` | changed (intentional, drop) | same as `/privacy`'s contract — cosmetic, no ported handler sends one. |
| Empty state text | same | `t(ctx, "no_rules")`, byte-identical to v1's catalog string, **not** the QA spec's paraphrase (see mismatch #2 above). |
| `substitute_user_tags` (all ten tag spellings) | same | ported verbatim into `_substitute_user_tags`. |
| Silent no-op when the substituted text is empty | same | `if not text: return` before the tagline check, matching `GroupShield.py:58-59`. |
| `"@MekhyW"`-suffix tagline suppression | same | ported as-is; a v1 quirk tied to the original maintainer's handle, preserved rather than genericised, since it is exactly what a group's stored rules text would already rely on. |
| Persistence: `group_rules(group_id, body, updated_by, updated_at)` | same | single-shard upsert on `group_id` (PK); `updated_by` accepts NULL for an anonymous admin. |
| "Not an admin" scenario text/video for `/newrules` | changed (intentional, fix) | copied QA scenario is provably v1's `/configurar` behaviour, not `/newrules`'s — see mismatch #1. Gherkin wording kept verbatim; its step definitions assert the real behaviour instead of the mismatched quote; a new scenario covers the real non-admin rejection. |
| Feature-flag gate | same | none — like `/privacy`, sits outside `functionsFun`/`functionsUtility`. |
| Cooldown/quota | same | none. |

## Known gaps for whoever owns the listed files

- `packages/cb-gateway/src/cb_gateway/handlers/__init__.py` does not import or
  register `rules.router` — needs `root.include_router(rules.router)` (plus the
  import) for `qa/test_core_rules.py` to pass end to end. Out of this port's
  file ownership.
- `docs/FEATURE-MAP.md`'s `core_rules` row could use a note pointing at
  mismatch #1 above (the "not an admin" scenario mismatch); this agent could
  not edit that file.
