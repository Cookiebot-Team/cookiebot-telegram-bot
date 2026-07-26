"""MediaService against a real database and a real (in-memory) object store.

Exercises the behaviour v1 got wrong: no dedupe, a full-collection load to pick a
random item, and no way to drop a group's media when it left.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from cb_core.storage import MediaService, store_from_uri

if TYPE_CHECKING:
    from qa.integration.factories import World

pytestmark = pytest.mark.integration


@pytest.fixture
def media(pg: ModuleType) -> MediaService:
    return MediaService(store_from_uri("memory://"))


class TestPut:
    def test_stores_and_registers(
        self,
        run: Callable[[Coroutine[Any, Any, Any]], Any],
        world: World,
        media: MediaService,
    ) -> None:
        user = world.add_user()
        ref = run(
            media.put(
                world.group_id,
                "photo",
                b"a photo",
                uploaded_by=user.user_id,
                content_type="image/jpeg",
            )
        )
        assert ref.group_id == world.group_id
        assert ref.byte_size == len(b"a photo")
        assert ref.blob_key.startswith("media/photo/")
        assert world.count("media_objects") == 1
        assert run(media.get_bytes(ref)) == b"a photo"

    def test_media_id_is_uuid7(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        ref = run(media.put(world.group_id, "photo", b"x"))
        assert ref.media_id.version == 7

    def test_same_bytes_twice_deduplicates(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        first = run(media.put(world.group_id, "photo", b"identical"))
        second = run(media.put(world.group_id, "photo", b"identical"))
        assert second.deduplicated
        assert second.media_id == first.media_id
        assert world.count("media_objects") == 1

    def test_different_bytes_are_separate_rows(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        run(media.put(world.group_id, "photo", b"one"))
        run(media.put(world.group_id, "photo", b"two"))
        assert world.count("media_objects") == 2

    def test_blob_is_shared_across_groups(
        self,
        run: Callable[[Coroutine[Any, Any, Any]], Any],
        world: World,
        media: MediaService,
        pg: ModuleType,
    ) -> None:
        """Two groups posting the same image store one blob and two references."""
        from qa.integration.factories import World

        other = World(run)
        other.setup()
        try:
            a = run(media.put(world.group_id, "photo", b"shared bytes"))
            b = run(media.put(other.group_id, "photo", b"shared bytes"))
            assert a.blob_key == b.blob_key
            assert a.media_id != b.media_id
            row = run(
                pg.fetchrow(
                    "SELECT count(*) AS n FROM media_blobs WHERE content_hash = $1",
                    a.content_hash,
                )
            )
            assert row["n"] == 1
        finally:
            other.teardown()

    def test_unknown_kind_rejected(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        with pytest.raises(ValueError):
            run(media.put(world.group_id, "hologram", b"x"))


class TestRandom:
    def test_returns_one_of_the_stored_items(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        for i in range(5):
            run(media.put(world.group_id, "photo", f"photo-{i}".encode()))
        picked = run(media.random(world.group_id))
        assert picked is not None
        assert picked.group_id == world.group_id

    def test_empty_group_returns_none(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        assert run(media.random(world.group_id)) is None

    def test_respects_kind_filter(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        run(media.put(world.group_id, "sticker", b"a sticker"))
        assert run(media.random(world.group_id, kinds=("photo",))) is None
        assert run(media.random(world.group_id, kinds=("sticker",))) is not None

    def test_sfw_filter_excludes_flagged_media(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        run(media.put(world.group_id, "photo", b"nsfw", sfw=False))
        assert run(media.random(world.group_id, sfw_only=True)) is None
        assert run(media.random(world.group_id, sfw_only=False)) is not None

    def test_scoped_to_the_group(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        from qa.integration.factories import World

        other = World(run)
        other.setup()
        try:
            run(media.put(world.group_id, "photo", b"ours"))
            assert run(media.random(other.group_id)) is None
        finally:
            other.teardown()


class TestLifecycle:
    def test_forget_group_drops_references(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        run(media.put(world.group_id, "photo", b"one"))
        run(media.put(world.group_id, "photo", b"two"))
        deleted = run(media.forget_group(world.group_id))
        assert deleted == 2
        assert world.count("media_objects") == 0

    def test_gc_removes_blobs_once_unreferenced(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        ref = run(media.put(world.group_id, "photo", b"gc me"))
        assert run(media.store.exists(ref.blob_key))

        run(media.forget_group(world.group_id))
        collected = run(media.collect_garbage())

        assert collected >= 1
        assert not run(media.store.exists(ref.blob_key))

    def test_gc_keeps_referenced_blobs(
        self, run: Callable[[Coroutine[Any, Any, Any]], Any], world: World, media: MediaService
    ) -> None:
        ref = run(media.put(world.group_id, "photo", b"keep me"))
        run(media.collect_garbage())
        assert run(media.store.exists(ref.blob_key))
