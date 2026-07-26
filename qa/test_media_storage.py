"""Step definitions for core_media_storage.feature.

Runs against the memory and local backends, which go through exactly the same
obstore code path as S3 and GCS — so the contract is covered without a cloud
account or a container.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core.storage import BlobStore, ObjectNotFoundError, StorageError, store_from_uri
from cb_core.storage.keys import hash_and_key

scenarios("core_media_storage.feature")


class Ctx:
    def __init__(self) -> None:
        self.store: BlobStore | None = None
        self.keys: list[str] = []
        self.payloads: list[bytes] = []
        self.error: Exception | None = None


@pytest.fixture
def ctx() -> Ctx:
    return Ctx()


@given(parsers.parse('a blob store backed by "{backend}"'))
def a_blob_store(ctx: Ctx, backend: str, tmp_path: Path) -> None:
    uri = "memory://" if backend == "memory" else f"file://{tmp_path}/{backend}"
    ctx.store = store_from_uri(uri)


def _store(
    ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], kind: str, data: bytes
) -> None:
    _, key = hash_and_key(kind, data)
    run(ctx.store.put(key, data))
    ctx.keys.append(key)
    ctx.payloads.append(data)


@when(parsers.parse("a photo of {size:d} bytes is stored"))
def store_sized_photo(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], size: int) -> None:
    _store(ctx, run, "photo", b"x" * size)


@when(parsers.parse('a photo with content "{content}" is stored'))
def store_photo(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], content: str) -> None:
    _store(ctx, run, "photo", content.encode())


@when(parsers.parse('a photo with content "{content}" is stored again'))
def store_photo_again(
    ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], content: str
) -> None:
    _store(ctx, run, "photo", content.encode())


@when(parsers.parse('a sticker with content "{content}" is stored'))
def store_sticker(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], content: str) -> None:
    _store(ctx, run, "sticker", content.encode())


@when("an object that was never stored is requested")
def request_missing(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    try:
        run(ctx.store.get("media/photo/zz/never-written.jpg"))
    except Exception as exc:  # noqa: BLE001 - the assertion is on the type
        ctx.error = exc


@when("the object is deleted twice")
def delete_twice(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    run(ctx.store.delete(ctx.keys[-1]))
    run(ctx.store.delete(ctx.keys[-1]))


@when("a signed URL is requested")
def request_signed_url(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    try:
        run(ctx.store.signed_url(ctx.keys[-1]))
    except Exception as exc:  # noqa: BLE001 - the assertion is on the type
        ctx.error = exc


@then("the object can be read back byte for byte")
def read_back(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    assert run(ctx.store.get(ctx.keys[-1])) == ctx.payloads[-1]


@then("the key is derived from the content hash")
def key_is_content_addressed(ctx: Ctx) -> None:
    expected_hash, _ = hash_and_key("photo", ctx.payloads[-1])
    assert expected_hash in ctx.keys[-1]


@then("both uploads resolve to the same key")
def same_key(ctx: Ctx) -> None:
    assert len(ctx.keys) == 2
    assert ctx.keys[0] == ctx.keys[1]


@then("the two uploads resolve to different keys")
def different_keys(ctx: Ctx) -> None:
    assert len(ctx.keys) == 2
    assert ctx.keys[0] != ctx.keys[1]


@then(parsers.parse('the key ends with "{suffix}"'))
def key_suffix(ctx: Ctx, suffix: str) -> None:
    assert ctx.keys[-1].endswith(suffix)


@then("the store reports it as not found")
def reports_not_found(ctx: Ctx) -> None:
    assert isinstance(ctx.error, ObjectNotFoundError)


@then("the object no longer exists")
def gone(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    assert not run(ctx.store.exists(ctx.keys[-1]))


@then("the store reports that it cannot sign URLs")
def cannot_sign(ctx: Ctx) -> None:
    assert isinstance(ctx.error, StorageError)
    assert "sign" in str(ctx.error).lower()
