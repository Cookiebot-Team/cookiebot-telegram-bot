# Pending `scripts/spec.py` rows — 8 v1 features with no row at all

Not applied. `scripts/spec.py` is being edited concurrently by another agent this
session; a working-tree write here would be a silent clobber. Apply once that
edit lands — this is the full audit finding, not a diff to reconcile against it.

## Source

feature-map.mdx §4 ("Implemented but NOT spec'd in QA") lists these eight v1
features with no corresponding row anywhere in `scripts/spec.py`'s `FEATURES`
tuple, confirmed by grepping `scripts/spec.py` for every plausible id/keyword
(no hits) and re-deriving file:line and triggers directly from
`../COOKIEBOT-Telegram-Group-Bot/Bot/{Miscellaneous,SocialContent,COOKIEBOT}.py`
rather than trusting the mdx table alone (it under-lists a few triggers, e.g.
`/edad`, `/gênero`, `/suerte`, `/ideadibujo`, `/cualquiercosa`, `/analisis`,
all present in `COOKIEBOT.py`'s dispatch `elif` chain but missing from the
mdx's "Trigger" column).

Ids follow the existing `x_*` convention already used for the other seven
"shipped in v1, never specified in QA" rows (`x_giveaways`, `x_reverse_search`,
etc.) — same section, same reason: real v1 code, zero QA-repo Gherkin.
`scripts/status.py`'s `MISSING_V1_INVENTORY` constant uses these exact ids;
`--strict-inventory` stops flagging each one the moment its row lands.

## Where to paste

Inside the `# fmt: off` / `# fmt: on` block, in the
`# ---------------------------------- shipped in v1, never specified in QA`
section, immediately after the existing `x_custom_commands` row and before
`x_webhub_login` (these eight are the same "spec debt" category as
`x_custom_commands`, just not yet milestoned into a specific slice — M3 matches
the rest of that section).

## The rows

```text
    Feature("x_age_guess", "fun", "Age guess (agify.io)", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:185-202", ("/idade", "/age", "/edad"),
            "agify.io name lookup, no auth; no QA scenario exists - write one"),
    Feature("x_gender_guess", "fun", "Gender guess (genderize.io)", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:204-224", ("/genero", "/gênero", "/gender"),
            "genderize.io name lookup, no auth; no QA scenario exists - write one"),
    Feature("x_unearth", "fun", "Unearth a random old message", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:325-333", ("/desenterrar", "/unearth"),
            "forwards a random message_id in [1, current]; no QA scenario exists - write one"),
    Feature("x_fortune_cookie", "fun", "Fortune cookie", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:359-375", ("/sorte", "/fortunecookie", "/suerte"),
            "animated GIF + locale-random fortune line from sorte.txt; "
            "no QA scenario exists - write one"),
    Feature("x_image_search", "util", "Image search (qualquer coisa)", "M3", Status.PLANNED,
            Layer.GATEWAY, "SocialContent.py:144-170", ("/qualquercoisa", "/anything", "/cualquiercosa"),
            "Google Custom Search Image API, sfw-gated; no QA scenario exists - write one"),
    Feature("x_drawing_idea", "fun", "Drawing idea prompt", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:137-143", ("/ideiadesenho", "/drawingidea", "/ideadibujo"),
            "signed URL from a GCS blob pool; no QA scenario exists - write one"),
    Feature("x_analysis", "util", "Message analysis (reply_to_message dump)", "M3", Status.PLANNED,
            Layer.GATEWAY, "Miscellaneous.py:71-81", ("/analise", "/analisis", "/analysis"),
            "dumps the raw Telegram reply_to_message payload back to chat; "
            "no QA scenario exists - write one"),
    Feature("x_sticker_autoreply", "fun", "Sticker DB auto-reply", "M3", Status.PLANNED,
            Layer.GATEWAY, "SocialContent.py:208-222", (),
            "passive: builds a sticker DB from sfw-group stickers, replies to any "
            "doc/sticker sent in reply to the bot; no QA scenario exists - write one"),
```

## After applying

- `python scripts/status.py --strict-inventory` should report zero
  `MISSING_V1_INVENTORY` findings.
- `docs/site/content/docs/scenario-coverage.mdx` §3 references this file by
  path; update that section once the rows are in (the table there stays
  accurate as a historical record either way, but the "not applied" framing
  should flip).
- Each row is `Status.PLANNED` with no Gherkin anywhere — same state as
  `x_giveaways` and friends. Writing the QA scenario is part of the port, per
  AGENTS.md §6, not a prerequisite to landing the spec row.
