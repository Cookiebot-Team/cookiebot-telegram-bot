"""`cb_worker.jobs.meme` and `cb_worker.meme_seed` — the composite, the
fallbacks, and the seeding contract.

Storage is the in-memory backend, so a real template can be put and fetched
without a cloud; the bot and the roster are fakes. What is asserted is v1's
behaviour: which faces fill which rectangles, what the caption reads, and the
three dead ends where v1 either says `meme_error` or (D-ME-1) crashes.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from cb_core import storage
from cb_core.meme_templates import MemeTemplate, all_templates
from cb_core.settings import Settings
from cb_worker import meme_seed
from cb_worker.jobs import meme as job


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


TEMPLATE = MemeTemplate(
    filename="two_faces.png",
    language="English",
    blob_count=2,
    blob_rects=((0, 0, 20, 20), (40, 40, 20, 20)),
)


class _Member:
    def __init__(self, user_id: int, username: str | None) -> None:
        self.user_id = user_id
        self.username = username


class _Photo:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


class _Photos:
    def __init__(self, file_ids: list[str]) -> None:
        self.photos = [[_Photo(fid)] for fid in file_ids]


class _Buffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeBot:
    def __init__(self, *, with_photos: set[int] | None = None) -> None:
        self.with_photos = with_photos if with_photos is not None else {1, 2, 3}
        self.photos_sent: list[dict[str, Any]] = []
        self.messages: list[str] = []

    async def get_user_profile_photos(self, user_id: int, limit: int = 1) -> _Photos:
        return _Photos([f"photo-{user_id}"] if user_id in self.with_photos else [])

    async def download(self, file_id: str) -> _Buffer:
        return _Buffer(_png(Image.new("RGB", (64, 64), "red")))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> None:
        self.photos_sent.append({"chat_id": chat_id, "file": photo, **kwargs})

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append(text)


@pytest.fixture
async def memory_store() -> Any:
    await storage.init_storage(Settings(storage_uri="memory://", traces_enabled=False))
    yield storage.store()
    await storage.close_storage()


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch) -> list[_Member]:
    people = [_Member(1, "alice"), _Member(2, "bob"), _Member(3, "carol")]

    async def _roster(group_id: int) -> tuple[_Member, ...]:
        return tuple(people)

    monkeypatch.setattr(job.members, "roster", _roster)
    return people


@pytest.fixture
def one_template(monkeypatch: pytest.MonkeyPatch) -> MemeTemplate:
    monkeypatch.setattr(job, "choose", lambda count, lang: TEMPLATE)
    return TEMPLATE


async def _seed_template(store: Any) -> None:
    await store.put(TEMPLATE.storage_key, _png(Image.new("RGB", (80, 80), "white")))


async def _run(bot: _FakeBot, tagged: list[str]) -> None:
    await job.compose_meme({"bot": bot}, group_id=-100, message_id=9, tagged=tagged, lang="en")


# --------------------------------------------------------------- pure pieces


def test_caption_keeps_v1s_trailing_space() -> None:
    """v1 builds it as `caption += f"@{chosen_member} "` (`:274`)."""
    assert job.caption_for(["alice", "bob"]) == "@alice @bob "


def test_faces_land_in_their_rectangles() -> None:
    template = Image.new("RGB", (80, 80), "white")
    red = Image.new("RGB", (5, 5), (255, 0, 0))
    blue = Image.new("RGB", (5, 5), (0, 0, 255))
    out = job.paste_faces(template, TEMPLATE.blob_rects, [red, blue])
    assert out.getpixel((10, 10)) == (255, 0, 0)
    assert out.getpixel((50, 50)) == (0, 0, 255)
    assert out.getpixel((30, 30)) == (255, 255, 255)  # untouched


def test_fewer_faces_than_rectangles_leaves_the_rest_alone() -> None:
    out = job.paste_faces(
        Image.new("RGB", (80, 80), "white"),
        TEMPLATE.blob_rects,
        [Image.new("RGB", (5, 5), (255, 0, 0))],
    )
    assert out.getpixel((50, 50)) == (255, 255, 255)


# ----------------------------------------------------------------- the job


async def test_a_tagged_member_fills_the_first_rectangle(
    memory_store: Any, roster: list[_Member], one_template: MemeTemplate
) -> None:
    """v1 drains the tagged list before falling back to the roster
    (`:257-268`), so the caption names the tagged member first."""
    await _seed_template(memory_store)
    bot = _FakeBot()
    await _run(bot, ["bob"])
    assert len(bot.photos_sent) == 1
    assert bot.photos_sent[0]["caption"].startswith("@bob ")
    assert bot.photos_sent[0]["reply_to_message_id"] == 9


async def test_untagged_rectangles_are_filled_from_the_roster(
    memory_store: Any, roster: list[_Member], one_template: MemeTemplate
) -> None:
    """v1's second loop. It is dead in v1 — it hands a member *dict* to a
    function expecting a username (D-ME-3) — so this is the drift, not the
    parity."""
    await _seed_template(memory_store)
    bot = _FakeBot()
    await _run(bot, [])
    assert len(bot.photos_sent) == 1
    assert bot.photos_sent[0]["caption"].count("@") == TEMPLATE.blob_count


async def test_a_member_without_a_photo_is_skipped_not_fatal(
    memory_store: Any, roster: list[_Member], one_template: MemeTemplate
) -> None:
    await _seed_template(memory_store)
    bot = _FakeBot(with_photos={3})
    await _run(bot, ["alice"])
    assert len(bot.photos_sent) == 1
    assert "@carol" in bot.photos_sent[0]["caption"]


async def test_nobody_with_a_photo_answers_meme_error(
    memory_store: Any, roster: list[_Member], one_template: MemeTemplate
) -> None:
    """v1's own dead end (`:269-272`)."""
    await _seed_template(memory_store)
    bot = _FakeBot(with_photos=set())
    await _run(bot, ["alice"])
    assert bot.photos_sent == []
    assert bot.messages and "profile picture" in bot.messages[0]


async def test_an_unseeded_store_answers_rather_than_failing(
    memory_store: Any, roster: list[_Member], one_template: MemeTemplate
) -> None:
    """Nothing was put in the store, so the template fetch misses. v1 could
    not hit this (the files sat next to the code) — a deployment that has not
    run `cb.py meme-seed` must still get an answer, not silence."""
    bot = _FakeBot()
    await _run(bot, ["alice"])
    assert bot.photos_sent == []
    assert bot.messages


async def test_no_suitable_template_answers_meme_error(
    memory_store: Any, roster: list[_Member], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-ME-1: v1 reads `contours_green` outside the `if` that assigns it
    (`:244-248`), so this case is a NameError and the group hears nothing."""
    monkeypatch.setattr(job, "choose", lambda count, lang: None)
    bot = _FakeBot()
    await _run(bot, ["alice"])
    assert bot.photos_sent == []
    assert bot.messages


# ------------------------------------------------------------------ seeding


def _fake_v1_checkout(root: Path, template: MemeTemplate) -> None:
    """One real catalog entry, present on disk where a v1 checkout would have
    it. The seeder iterates the *catalog*, so a made-up filename would simply
    never be looked for."""
    directory = root / meme_seed.V1_SUBPATH / template.language
    directory.mkdir(parents=True, exist_ok=True)
    (directory / template.filename).write_bytes(_png(Image.new("RGB", (8, 8), "white")))


async def test_seeding_copies_then_skips(memory_store: Any, tmp_path: Path) -> None:
    real = all_templates()[0]
    _fake_v1_checkout(tmp_path, real)

    first = await meme_seed.seed(tmp_path)
    assert first.copied == 1
    assert first.missing, "the other 800 templates are not in this tmp dir"
    assert await memory_store.exists(real.storage_key)

    second = await meme_seed.seed(tmp_path)
    assert second.copied == 0, "a re-run must not rewrite what is already there"
    assert second.skipped == 1


async def test_dry_run_writes_nothing(memory_store: Any, tmp_path: Path) -> None:
    real = all_templates()[0]
    _fake_v1_checkout(tmp_path, real)

    report = await meme_seed.seed(tmp_path, dry_run=True)
    assert report.copied == 1
    assert not await memory_store.exists(real.storage_key)


async def test_verify_reports_what_the_store_is_missing(memory_store: Any, tmp_path: Path) -> None:
    """`--verify` reads nothing from v1 — it answers "is the destination
    complete?", which is the question after a seeding run."""
    report = await meme_seed.seed(tmp_path, verify=True)
    assert not report.ok
    assert len(report.missing or []) == len(all_templates())


def test_source_path_ignores_the_csvs_working_directory_relative_column() -> None:
    """The CSV's `full_path` is `Static/Meme/...`, relative to v1's *working
    directory* — it only resolves standing in `Bot/`."""
    assert meme_seed.source_path(Path("/v1"), TEMPLATE) == Path(
        "/v1/Bot/Static/Meme/English/two_faces.png"
    )
