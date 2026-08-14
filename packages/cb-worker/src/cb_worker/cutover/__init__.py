"""One operator-facing command for cutover day: v1 -> v2, in order, with progress.

`python scripts/cb.py cutover` composes the tools that already exist —
`cb_core.migrations` (schema), `cb_worker.importer` (Mongo), `cb_worker.bucket_export`
(v1's private GCS bucket) and `cb_worker.meme_seed` (v1's meme templates checkout) —
into one run instead of five commands typed in the right order under pressure.
It replaces none of them: every task in `scripts/cb.py` this composes still
works standalone, for exactly the cases each one already covered (a mid-week
Mongo delta sync, a `--verify` re-check).

Every step this module drives is already idempotent (see each tool's own
module docstring), which is what makes `cutover` itself safe to run more than
once — the second run of the whole thing costs a few "already there" checks,
never a duplicate or a clobbered v2-side edit.

Six steps, always in this order:

    preflight - read-only. What's reachable, what's configured, what's missing.
    schema    - `alembic upgrade head`, reporting the revision before and after.
    mongo     - v1 MongoDB (or a mongodump directory) -> Citus.
    bucket    - v1's private GCS bucket -> v2 object storage.
    memes     - v1's meme template checkout -> v2 object storage.
    verify    - read-only. Row counts, object counts, the alembic revision.

`--only`/`--skip` (see `resolve_steps`) narrow which of the six actually run,
without changing that relative order. A step that fails is recorded and the
run moves on to the next one — same "one bad thing must not cost the rest"
contract every underlying tool already keeps for its own units of work (a
Mongo collection, a GCS blob) — so the operator sees everything that *could*
run this time, not just the first failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Every step `cutover` knows how to run, in the fixed order they execute in.
#: `--only`/`--skip` narrow this set; they never reorder it.
StepName = Literal["preflight", "schema", "mongo", "random", "bucket", "memes", "verify"]

#: `random` runs after `mongo` because `media_objects.group_id` is a foreign
#: key to `groups`: the pointers it backfills belong to groups the import has
#: to have created first (`cb_worker/backfill/random_media.py`).
STEP_ORDER: tuple[StepName, ...] = (
    "preflight",
    "schema",
    "mongo",
    "random",
    "bucket",
    "memes",
    "verify",
)

#: A step's own outcome. "skipped" covers both "not selected" and "selected but
#: nothing to do" (no Mongo source configured, no bucket destination configured)
#: — neither is a failure, and folding them together is what lets a full run
#: against a partially-configured environment still exit 0.
StepStatus = Literal["ok", "skipped", "failed"]

#: One preflight check's own verdict — deliberately a different type from
#: `StepStatus` ("skip" not "skipped") so a reader never confuses a row of the
#: preflight table with a step of the summary table; they answer different
#: questions ("is X reachable?" vs "did step Y succeed?").
CheckStatus = Literal["ok", "skip", "fail"]


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One row of the preflight table: what was checked, and what was found.

    Preflight never writes, so `detail` is always the result of a read (a
    connection, a directory listing, an env var lookup) — never "would write
    ...", which is what `StepResult.detail` says for a real step.
    """

    name: str
    status: CheckStatus
    detail: str


@dataclass(slots=True)
class StepResult:
    """One row of the final summary table.

    `headline` is deliberately a string, not a fixed numeric field: "rows
    written", "objects copied" and "revision unchanged" are different units for
    different steps, and forcing them into one `int` would either lose the unit
    or force every step to invent one that does not fit (schema has no natural
    count; `1 upgrade` vs `0` is not the interesting fact, the revision is).
    """

    step: StepName
    status: StepStatus
    duration_s: float
    headline: str
    detail: str = ""


@dataclass(slots=True)
class CutoverReport:
    """What one `cutover` invocation did, step by step."""

    preflight_checks: list[PreflightCheck] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)

    def any_failed(self) -> bool:
        return any(s.status == "failed" for s in self.steps)


class StepSelectionError(ValueError):
    """An unknown step name in `--only`/`--skip` — a user error, not a bug."""


def _parse_step_names(raw: str) -> tuple[StepName, ...]:
    """`"mongo,bucket"` -> `("mongo", "bucket")`, validated against `STEP_ORDER`.

    Raises `StepSelectionError` naming the exact bad token rather than dumping
    the whole invalid list, because with a typo like `bucet` that is what tells
    the operator which one to fix.
    """
    names: list[StepName] = []
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        if token not in STEP_ORDER:
            raise StepSelectionError(
                f"unknown cutover step {token!r}; valid steps are: {', '.join(STEP_ORDER)}"
            )
        names.append(token)  # mypy narrows str -> StepName from the membership check above
    return tuple(names)


def resolve_steps(only: str = "", skip: str = "") -> tuple[StepName, ...]:
    """The steps to run, in `STEP_ORDER`: `only` narrows to that subset (default:
    every step), then `skip` removes from it. Both are comma-separated and both
    validate every name they see, so `cutover --only mogno` fails clearly
    instead of silently running nothing.
    """
    only_names = _parse_step_names(only) if only else STEP_ORDER
    skip_names = set(_parse_step_names(skip)) if skip else set()
    return tuple(s for s in STEP_ORDER if s in only_names and s not in skip_names)


__all__ = [
    "STEP_ORDER",
    "CheckStatus",
    "CutoverReport",
    "PreflightCheck",
    "StepName",
    "StepResult",
    "StepSelectionError",
    "StepStatus",
    "resolve_steps",
]
