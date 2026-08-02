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

__all__ = ["CALLADMS_NOTIFY_ADMINS", "EVERYONE_FANOUT"]
