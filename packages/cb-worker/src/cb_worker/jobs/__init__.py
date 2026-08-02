"""arq job implementations that are not cron — one module per feature's fan-out.

`cb_worker/main.py` still owns `WorkerSettings` and the cron schedule; a job
here is imported there and appended to `functions`, never the other way round
(a job module must not import `cb_worker.main` — see `everyone.py`'s docstring).
"""

from __future__ import annotations
