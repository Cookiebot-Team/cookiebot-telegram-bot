"""Who in a group has a birthday on a given day — the read `util_birthday`/
`util_nextbirthday` share.

v1's own two consumers of this idea (`Birthdays.py:20` and `:109`, both
`get_request_backend(f"users?birthdate={date}")`) each re-filtered the
result down to "and is in this group" by hand, against a bare list of
usernames from a separate `registers/{id}` call — the same N+1-shaped
registry read `cb_core.members.roster`'s own docstring already tells this
story for. Here it is one single-shard join, the identical shape `roster`
already established: `group_members` is distributed on `group_id` and
colocated with `groups`, `users` is a **reference table** (replicated to
every node), so `group_id` first in the `WHERE`, joined to `users` by its
primary key, is node-local — no new Citus concern, this query is
structurally the same as `roster`'s, filtered further by
`birth_month`/`birth_day` (both `GENERATED` columns, `0001_initial_schema.py:82-83`,
backed by `users_birthday_idx`) instead of "everyone."

Neither v2 nor v1 collects a birthdate through any code path that still
runs (`.specs/features/util_birthday/spec.md`'s "collection-mechanism"
section) — what this module reads is whatever survived the one-time Mongo
import (`cb_worker/importer/mappers.py:map_users`) for a migrated group, or
`NULL` for anyone new. That is a deliberate, approved scope decision, not an
oversight of this module.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

from cb_core import db, locales
from cb_core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BirthdayPerson:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None


_MEMBERS_WITH_BIRTHDAY = """
SELECT u.user_id, u.username, u.first_name, u.last_name
  FROM group_members gm
  JOIN users u ON u.user_id = gm.user_id
 WHERE gm.group_id = $1
   AND gm.left_at IS NULL
   AND u.birth_month = $2
   AND u.birth_day = $3
 ORDER BY u.user_id
"""


async def members_with_birthday(group_id: int, month: int, day: int) -> tuple[BirthdayPerson, ...]:
    """Registered, still-present members of `group_id` whose `birth_month`/
    `birth_day` matches. A database failure degrades to "nobody" rather than
    a 500 — the same posture `members.roster` already takes for the
    identical reason (a fun/util command must not crash a reply on a db blip).
    """
    try:
        rows = await db.fetch(
            _MEMBERS_WITH_BIRTHDAY, group_id, month, day, name="birthdays_members"
        )
    except Exception as exc:  # noqa: BLE001 - degrade, don't 500 a fun/util command
        log.warning("birthdays.query_failed", error=str(exc))
        return ()
    return tuple(
        BirthdayPerson(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
        )
        for row in rows
    )


_ALL_USERS_WITH_BIRTHDAY = """
SELECT user_id, username, first_name, last_name
  FROM users
 WHERE birth_month = $1
   AND birth_day = $2
 ORDER BY user_id
"""


async def all_users_with_birthday(month: int, day: int) -> tuple[BirthdayPerson, ...]:
    """**Not** filtered to one group — v1's `next_birthdays` reads the raw
    backend response (`GET users?birthdate=`, `Birthdays.py:109`) with no
    group filter at all, unlike `birthday()`'s own collage, which explicitly
    filters the same kind of list down to "and is in this group"
    (`Birthdays.py:36-39`, `not in [x['user'] for x in users_in_group]:
    continue`). This is a genuine, confirmed difference in scope between
    v1's two birthday features, not an oversight in this module — `bday.next`'s
    own header text, `"UPCOMING BIRTHDAYS (all groups)"`, is honestly
    describing exactly this: `/nextbirthday` in v1 shows every user in the
    whole system with an upcoming birthday, regardless of which group asked.
    Preserved as-is (AGENTS.md: v1 code wins for observable behaviour); a
    privacy-relevant scope decision, called out plainly in
    `docs/contracts/util_nextbirthday.md`, not silently narrowed to
    "this group only" on the assumption that the wider scope was a mistake.

    Safe under AGENTS.md §4 without a `group_id`: `users` is a **reference**
    table, replicated to every node, so this is a node-local scan wherever
    it runs — the Citus rule about a distributed-table query needing
    `group_id` in the `WHERE` does not apply to a reference table at all.
    """
    try:
        rows = await db.fetch(_ALL_USERS_WITH_BIRTHDAY, month, day, name="birthdays_all_users")
    except Exception as exc:  # noqa: BLE001 - degrade, don't 500 a fun/util command
        log.warning("birthdays.all_users_query_failed", error=str(exc))
        return ()
    return tuple(
        BirthdayPerson(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
        )
        for row in rows
    )


def display_name(person: BirthdayPerson) -> str:
    """v1's identical fallback, used by both `make_birthday_caption`
    (`Birthdays.py:93`) and `next_birthdays` (`Birthdays.py:117`) —
    `@username` when present, else `"{firstName} {lastName}"`. Ported once
    here instead of twice."""
    if person.username:
        return f"@{person.username}"
    return f"{person.first_name or ''} {person.last_name or ''}".strip()


# --------------------------------------------------------------- catalog reads


def _bday_catalog(lang: str) -> dict[str, object]:
    """The nested `"bday"` object (`title`/`cta`/`closing`/`next`) —
    `cb_core.locales.get` only resolves flat keys, and `"bday.title"` etc. are
    not flat keys, they are dotted-looking names that do not exist in a JSON
    file whose actual shape is `{"bday": {"title": ..., ...}}`. Same gap
    `groupguardian.py`'s `_captcha_strings` and `battle.py`'s `_catalog_choice`
    already document for their own nested/list-valued keys, cast-and-fallback,
    same shape.
    """
    raw = cast(dict[str, object], locales.catalog(lang))
    value = raw.get("bday")
    if not isinstance(value, dict):
        raw_en = cast(dict[str, object], locales.catalog("en"))
        value = raw_en.get("bday", {})
    return cast(dict[str, object], value)


def bday_title(lang: str) -> str:
    """v1: `i18n.get("bday.title", ...)` (`Birthdays.py:17`) — the bare-`/birthday`
    prompt, see `docs/contracts/util_birthday.md`'s recorded QA conflict."""
    return cast(str, _bday_catalog(lang).get("title", ""))


def bday_cta(lang: str, *, names: str, rng: random.Random | None = None) -> str:
    """v1: a random `bday.cta` line, `%(names)s` substituted
    (`Birthdays.py:90-94`). v1's loop reassigns `caption` on every iteration
    while building `names` incrementally, so only the *last* iteration's
    random pick (against the fully-joined `names` string) is ever kept —
    the observable result is "one random line, the complete names string,"
    reproduced directly here rather than v1's incidental extra RNG draws,
    which have no effect a user could ever observe."""
    choices = cast(list[str], _bday_catalog(lang).get("cta", []))
    if not choices:
        return ""
    picker = rng.choice if rng is not None else random.choice
    template = picker(choices)
    try:
        return template % {"names": names}
    except (KeyError, ValueError, TypeError):
        return template


def bday_closing(lang: str, *, date: str) -> str:
    """v1: `i18n.get("bday.closing", ..., date=...)` (`Birthdays.py:95`)."""
    template = cast(str, _bday_catalog(lang).get("closing", ""))
    try:
        return template % {"date": date}
    except (KeyError, ValueError, TypeError):
        return template


def bday_next_header(lang: str) -> str:
    """v1: `i18n.get("bday.next", ...)` (`Birthdays.py:108`) — localised per
    language (unlike the per-day line below), but every language's wording
    still says "(all groups)", stale for this port's single-group scope —
    a cosmetic label, not a behaviour bug."""
    return cast(str, _bday_catalog(lang).get("next", ""))


# ------------------------------------------------------------ /nextbirthday text


async def next_birthdays_text(lang: str, today: date) -> str:
    """v1's `next_birthdays` (`Birthdays.py:104-117`), byte-for-byte —
    `bday_next_header` then, for `offset` in `1..4`, a **literal** (not
    `i18n`) `f"{offset} dias:\n"` line — hardcoded Portuguese regardless of
    `lang`, `Birthdays.py:110` — followed by one `display_name` per person
    whose birthday falls on `today + offset`, or `"- \n"` if nobody does.

    No `group_id` parameter, on purpose: `all_users_with_birthday` is
    deliberately **not** group-scoped, matching v1's own `next_birthdays`
    exactly — see that function's docstring for why this is confirmed v1
    behaviour, not an oversight.

    Shared by `cb_gateway.handlers.nextbirthday` (the manual command) and
    `cb_worker.jobs.birthday.next_birthdays_followup` (the deferred
    replacement for v1's `threading.Timer`), so both render identically —
    v1 called the same function from both its own dispatch and its own timer.
    """
    text = bday_next_header(lang)
    for offset in range(1, 5):
        target = today + timedelta(days=offset)
        people = await all_users_with_birthday(target.month, target.day)
        text += f"{offset} dias:\n"
        if not people:
            text += "- \n"
        else:
            for person in people:
                text += f"{display_name(person)}\n"
        text += "\n"
    return text


__all__ = [
    "BirthdayPerson",
    "all_users_with_birthday",
    "bday_closing",
    "bday_cta",
    "bday_next_header",
    "bday_title",
    "display_name",
    "members_with_birthday",
    "next_birthdays_text",
]
