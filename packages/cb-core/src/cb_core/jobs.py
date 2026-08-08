"""arq job-name constants, shared between `cb-gateway` (enqueues) and `cb-worker`
(registers the function and consumes it).

A job name is a bare string on the wire — `cb_gateway.queue.enqueue(name, ...)`
on one side, `WorkerSettings.functions` on the other (`cb_worker/main.py`). With
no shared source, a rename on either side desynchronises silently: the gateway
enqueues a name arq has no function for, the job sits until it hits the retry
limit, and nothing at the call site says why. Importing the constant from here
instead of typing the literal is what keeps that impossible.
"""

from __future__ import annotations

#: `util_everyone`'s DM fan-out (`cb_worker/jobs/everyone.py`). First consumer
#: of the gateway->worker enqueue wiring (`cb_gateway/queue.py`).
EVERYONE_FANOUT = "everyone_fanout"

#: `util_calladms`'s DM half (`cb_worker/jobs/calladms.py`), enqueued from
#: `cb_gateway/handlers/calladms.py` once a confirmed `/adm` press has pinged
#: the group. Second consumer of the same wiring.
CALLADMS_NOTIFY_ADMINS = "notify_admins_of_call"

#: `util_youtube`'s search + reply (`cb_worker/jobs/youtube.py`), enqueued from
#: `cb_gateway/handlers/youtube.py` — an external API call, AGENTS.md §2.4's
#: "nothing slow on the reply path" applied to the third consumer of this wiring.
YOUTUBE_SEARCH = "youtube_search"

#: `util_birthday`'s collage — image compositing, AGENTS.md §2.4
#: (`cb_worker/jobs/birthday.py`), enqueued from
#: `cb_gateway/handlers/birthday.py`.
BIRTHDAY_COLLAGE = "birthday_collage"

#: The 900-second follow-up v1 scheduled with an in-process `threading.Timer`
#: (a restart silently drops it — `.specs/features/util_birthday/spec.md`'s
#: D-BD-2). `cb_worker/jobs/birthday.py:post_birthday_collage` enqueues this
#: one with arq's native `_defer_by` instead — durable, in Redis, not memory.
NEXT_BIRTHDAYS_FOLLOWUP = "next_birthdays_followup"

#: `util_postforwarder`'s render-and-fan-out (`cb_worker/jobs/publisher.py`),
#: enqueued from `cb_gateway/handlers/publisher.py` when the owner approves a
#: post. Two translations, N currency lookups, two media sends and a write per
#: consenting group — AGENTS.md §2.4 in every one of its clauses at once.
#: The delivery cron in the same module needs no constant: it is never
#: enqueued by name, only registered as a `cron_job`.
PUBLISHER_APPROVE = "publisher_approve"

#: `x_reverse_search`'s SauceNAO lookup (`cb_worker/jobs/reverse_search.py`),
#: enqueued from `cb_gateway/handlers/reverse_search.py`. In the worker for two
#: reasons: it is an unbounded external call (AGENTS.md §2.4), and the image
#: bytes are downloaded where they are used, so nothing but scalars goes on the
#: queue -- and, critically, so no Telegram file URL carrying the bot token is
#: ever constructed (spec D-RS-1).
REVERSE_SEARCH = "reverse_search"

#: `fun_meme`'s compositing pass (`cb_worker/jobs/meme.py`), enqueued from
#: `cb_gateway/handlers/meme.py`. A template fetch from object storage, N
#: profile-photo downloads and a Pillow paste — `scripts/spec.py`'s row for
#: this feature already said "image compositing is a worker job, not a
#: reply-path call".
COMPOSE_MEME = "compose_meme"

__all__ = [
    "BIRTHDAY_COLLAGE",
    "CALLADMS_NOTIFY_ADMINS",
    "COMPOSE_MEME",
    "EVERYONE_FANOUT",
    "NEXT_BIRTHDAYS_FOLLOWUP",
    "PUBLISHER_APPROVE",
    "REVERSE_SEARCH",
    "YOUTUBE_SEARCH",
]
