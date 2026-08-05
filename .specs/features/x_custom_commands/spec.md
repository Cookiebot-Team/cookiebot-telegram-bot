# x_custom_commands — Specify

**Feature id:** `x_custom_commands` · **Milestone:** M3 · **Kind:** v1 port
**v1 source:** `Bot/Miscellaneous.py:145-158` (`custom_command`), with its
trigger list at `Bot/Miscellaneous.py:23`, dispatched
`Bot/COOKIEBOT.py:281-282`.

## Status: BLOCKED — the same GCS bucket as `fun_death`, one step worse

No `design.md` and no `tasks.md`, for the same reason `fun_death` has a design
written but nothing executable: there is nothing to build against yet.

## Goal

Per-group custom photo commands. A group gets `/<anything>` and the bot answers
with a captioned image from a pool named after that command.

## The blocker

`fun_death`'s pool is a live listing of a private GCS bucket. This feature's
pool is too — **and so is its list of triggers**:

```python
# Bot/Miscellaneous.py:23 — the trigger list itself, built at import
custom_commands = list(
    dict.fromkeys(
        [folder.name.split("/")[1] for folder in storage_bucket.list_blobs(prefix="Custom/")]
    )
)

# Bot/Miscellaneous.py:147 — the images, per invocation
bloblist = list(
    storage_bucket.list_blobs(
        prefix="Custom/"
        + msg["text"]
        .replace("/", "")
        .replace("@CookieMWbot", "")
        .replace("@pawstralbot", "")
        .split()[0]
    )
)
```

`storage_bucket` is `storage_client.get_bucket("cookiebot-bucket")`
(`universal_funcs.py:27`) — the same private bucket, read through 15-minute
signed URLs.

That is a strictly harder blocker than `fun_death`'s. For `fun_death` the
trigger (`/death`, `/morte`, `/muerte`) and the caption template are both in
the source, so only the images are missing and the port is mechanical once they
land. Here **the command names are folder names in the bucket**. Without the
export there is no trigger list to put in `COMMAND_ALIASES`, no way to know how
many commands exist, and no way to write a QA scenario that names one.
`AGENTS.md` §2.1 — "no new command name without an alias" — cannot even be
evaluated.

Checked, same three places as `fun_death`'s spec:

1. `../COOKIEBOT-Telegram-Group-Bot/Bot/Static/` — `locales/`, `Meme/`,
   `reclamacao/`, and loose files. No `Custom/`.
2. Nothing named `Custom*` anywhere in the checkout (`find . -iname "Custom*"`).
3. No credential for `cookiebot-bucket` in this repo or environment.

## Prerequisite

Someone with access exports the bucket's `Custom/` prefix — both the folder
names and their contents. Tracked together with `fun_death`'s `Death/`,
`fun_battle`'s `Fight/English` and `Fight/Portuguese`, and
`fun_partneredcons`'s five `Countdown/*` prefixes: one export unblocks four
features.

## What is known about the behaviour, for when it lands

| Aspect | v1 behaviour (file:line) |
|---|---|
| Trigger | any `/<name>` where `name` is a `Custom/` folder, after stripping `/`, `@CookieMWbot` and `@pawstralbot` (`COOKIEBOT.py:281`) |
| Preconditions | `functionsFun` only (`COOKIEBOT.py:281`) — no admin check |
| Selection | `/<name> <n>` picks image `n` when the second word `.isdigit()`; otherwise `random.randint(0, len(bloblist)-1)` (`:148-151`) |
| Output | `send_photo` with a 15-minute signed URL, replying to the message, captioned `i18n.get("custom_photo", name=<name>.capitalize(), image_id=<n>)` (`:152-158`) |
| Side effects | `send_chat_action(chat_id, 'upload_photo')` (`:146`) |
| Persistence | none — the bucket *is* the state |

Two defects to decide on at port time, both already visible:

| id | Defect | Likely verdict |
|---|---|---|
| D-CC-1 | `bloblist[image_id]` with a user-supplied `n` and no bounds check — `/<name> 999` raises `IndexError` into the global traceback handler, `/<name> 0` on an empty folder raises too (`random.randint(0, -1)`) | **fix** — clamp, or answer the "no such image" string |
| D-CC-2 | `custom_commands` is computed once at import, so a folder added to the bucket needs a restart of all five processes to become a command | **fix** — this is FEATURE-MAP D6's shape; v2 would read it through a TTL'd cache |

## Note on the "tenant handler packs" framing

`scripts/spec.py` calls this "the seed of tenant handler packs", and that is
still the right read — a per-tenant set of commands whose definitions live
outside the code is exactly what `platform_tenancy`'s pending half needs. But
the dependency runs the other way round from how the M3 ordering implies:
this feature cannot demonstrate the pattern until its assets exist, so
`platform_tenancy` should not wait on it.
