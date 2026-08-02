"""`cb_core.errors` — wrapping, and what the chain renders as.

The failure these exist for: a foreign-key rejection that read as
`TelegramBadRequest` in the chat, as a Postgres DETAIL line in the database log,
and as `error=""` in the application log — three renderings of one interaction,
none of which named the work being done.
"""

from __future__ import annotations

import asyncio

import pytest

from cb_core import errors


def _fk_failure() -> Exception:
    return ValueError(
        'insert or update on table "group_configs_102052" violates foreign key '
        'constraint "group_configs_group_id_fkey_102052"'
    )


class TestFailAs:
    def test_wraps_the_original_as_the_cause(self) -> None:
        original = _fk_failure()
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("group_config.set_config", group_id=-55, columns="language"),
        ):
            raise original
        assert caught.value.__cause__ is original
        assert caught.value.operation == "group_config.set_config"
        assert caught.value.context == {"group_id": -55, "columns": "language"}

    def test_the_message_names_the_work(self) -> None:
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("group_config.set_config", group_id=-55, columns="language"),
        ):
            raise _fk_failure()
        assert str(caught.value) == "group_config.set_config(group_id=-55, columns=language)"

    def test_none_valued_context_is_dropped(self) -> None:
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("job.run", name="rollup", group_id=None),
        ):
            raise _fk_failure()
        assert str(caught.value) == "job.run(name=rollup)"

    def test_success_is_untouched(self) -> None:
        with errors.fail_as("group_config.set_config", group_id=-55):
            result = 1 + 1
        assert result == 2

    def test_an_already_described_failure_is_not_wrapped_twice(self) -> None:
        """An outer layer re-describing an inner one adds a link that says
        nothing the next one does not."""
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("outer", group_id=-55),
            errors.fail_as("inner", group_id=-55),
        ):
            raise _fk_failure()
        assert caught.value.operation == "inner"
        assert len(errors.chain(caught.value)) == 2

    def test_cancellation_passes_through(self) -> None:
        """A cancelled task is not a failure of the operation. Wrapping it would
        turn an orderly shutdown into a reported error — and, worse, into one
        that no longer matches `except CancelledError`."""
        with pytest.raises(asyncio.CancelledError), errors.fail_as("job.run", name="rollup"):
            raise asyncio.CancelledError


class TestChain:
    def test_outermost_first_innermost_last(self) -> None:
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("group_config.set_config", group_id=-55),
        ):
            raise _fk_failure()
        links = errors.chain(caught.value)
        assert [link["type"] for link in links] == ["CbError", "ValueError"]
        assert links[0]["operation"] == "group_config.set_config"
        assert links[0]["context"] == {"group_id": -55}
        assert "group_configs_102052" in links[1]["message"]

    def test_follows_implicit_context_when_nobody_wrote_from(self) -> None:
        """`raise` inside an `except` sets `__context__`, not `__cause__`. A
        chain that only followed `__cause__` would stop at the first link and
        report the least informative half of the failure."""
        try:
            try:
                raise _fk_failure()
            except ValueError:
                raise RuntimeError("retry also failed")  # noqa: B904 - the point of the test
        except RuntimeError as exc:
            links = errors.chain(exc)
        assert [link["type"] for link in links] == ["RuntimeError", "ValueError"]

    def test_long_chains_are_truncated(self) -> None:
        exc: Exception = ValueError("root")
        for i in range(errors.MAX_LINKS + 5):
            try:
                raise RuntimeError(f"link {i}") from exc
            except RuntimeError as raised:
                exc = raised
        assert len(errors.chain(exc)) == errors.MAX_LINKS

    def test_messages_are_collapsed_and_trimmed(self) -> None:
        exc = ValueError("a\n  b" + "x" * errors.MAX_MESSAGE)
        (link,) = errors.chain(exc)
        assert link["message"].startswith("a b")
        assert len(link["message"]) == errors.MAX_MESSAGE
        assert link["message"].endswith("…")

    def test_nothing_renders_as_nothing(self) -> None:
        assert errors.chain(None) == ()
        assert errors.render(None) == ""
        assert errors.reason(None) == ""
        assert errors.root(None) is None


class TestRender:
    def test_reads_right_to_left_as_what_broke(self) -> None:
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("group_config.set_config", group_id=-55, columns="language"),
        ):
            raise _fk_failure()
        rendered = errors.render(caught.value)
        assert rendered.startswith(
            "CbError: group_config.set_config(group_id=-55, columns=language)"
        )
        assert " <- ValueError: insert or update on table" in rendered

    def test_reason_is_the_innermost_failure(self) -> None:
        """What goes in the chat. The outer link names the operation, which the
        operator wants and the user cannot act on; the inner one is the fact."""
        with (
            pytest.raises(errors.CbError) as caught,
            errors.fail_as("group_config.set_config", group_id=-55),
        ):
            raise _fk_failure()
        assert errors.reason(caught.value).startswith("insert or update on table")

    def test_reason_falls_back_to_the_type_when_there_is_no_message(self) -> None:
        assert errors.reason(TimeoutError()) == "TimeoutError"

    def test_root_is_the_innermost_exception_object(self) -> None:
        original = _fk_failure()
        with pytest.raises(errors.CbError) as caught, errors.fail_as("group_config.set_config"):
            raise original
        assert errors.root(caught.value) is original
