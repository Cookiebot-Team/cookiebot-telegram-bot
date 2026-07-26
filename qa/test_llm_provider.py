"""Step definitions for core_llm_provider.feature.

The provider is stubbed — these scenarios are about *routing, gating and failure
handling*, which is our code, not the vendor's. Live-model behaviour is not
something an acceptance suite should assert.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from cb_core.llm import LLMError, LLMRouter, LLMUnavailableError, Message, TaskConfig, Usage
from cb_core.llm.anthropic_provider import AnthropicProvider
from cb_core.llm.types import Completion, Transcript

scenarios("core_llm_provider.feature")


class StubProvider:
    def __init__(self) -> None:
        self.refuse_category: str | None = None
        self.always_fail = False
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "stub"

    async def complete(
        self, messages: Sequence[Message], **kwargs: str | int | float | bool | None
    ) -> Completion:
        self.calls.append(kwargs)
        if self.always_fail:
            raise LLMError("provider is down")
        return Completion(
            text="" if self.refuse_category else "a reply",
            model=kwargs["model"],
            provider="stub",
            usage=Usage(input_tokens=120, output_tokens=45),
            stop_reason="refusal" if self.refuse_category else "end_turn",
            refusal_category=self.refuse_category,
        )

    async def stream(
        self, messages: Sequence[Message], **kwargs: str | int | float | bool | None
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        return 1

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "a.ogg",
        language: str | None = None,
    ) -> Transcript:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class Ctx:
    def __init__(self) -> None:
        self.provider = StubProvider()
        self.router: LLMRouter | None = None
        self.completion: Completion | None = None
        self.error: Exception | None = None
        self.params: dict | None = None


@pytest.fixture
def ctx() -> Ctx:
    return Ctx()


@given(parsers.parse('the "{task}" task is configured for provider "{provider}" model "{model}"'))
def configure_task(ctx: Ctx, task: str, provider: str, model: str) -> None:
    ctx.router = LLMRouter(
        {"stub": ctx.provider},
        {task: TaskConfig(provider=provider, model=model, max_tokens=64)},
        record_usage=False,
    )


@given(parsers.parse('the provider will refuse the next request with category "{category}"'))
def will_refuse(ctx: Ctx, category: str) -> None:
    ctx.provider.refuse_category = category


@given("the provider fails every request")
def will_fail(ctx: Ctx) -> None:
    ctx.provider.always_fail = True


@given("the default model catalog")
def default_catalog(ctx: Ctx) -> None:
    return None


@when(parsers.parse('the bot asks the "{task}" task to answer "{prompt}"'))
def ask(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], task: str, prompt: str) -> None:
    assert ctx.router is not None
    try:
        ctx.completion = run(ctx.router.complete(task, [Message(role="user", content=prompt)]))
    except Exception as exc:  # noqa: BLE001 - the assertion is on the type
        ctx.error = exc


@when(parsers.parse('the bot asks the "{task}" task {count:d} times'))
def ask_repeatedly(
    ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any], task: str, count: int
) -> None:
    assert ctx.router is not None
    for _ in range(count):
        try:
            run(ctx.router.complete(task, [Message(role="user", content="hi")]))
        except Exception as exc:  # noqa: BLE001 - failures are the point here
            ctx.error = exc


@when(parsers.parse('a request is prepared for "{model}" with temperature {temperature:f}'))
def prepare_request(ctx: Ctx, model: str, temperature: float) -> None:
    provider = AnthropicProvider(api_key="not-used-offline")
    ctx.params, _ = provider.build_request(
        [Message(role="user", content="hi")],
        model=model,
        max_tokens=100,
        system=None,
        temperature=temperature,
        effort=None,
        thinking=None,
    )


@then(parsers.parse('the call is made with model "{model}"'))
def called_with_model(ctx: Ctx, model: str) -> None:
    assert ctx.provider.calls, f"provider was never called (error: {ctx.error})"
    assert ctx.provider.calls[-1]["model"] == model


@then("a reply is returned")
def reply_returned(ctx: Ctx) -> None:
    assert ctx.completion is not None
    assert ctx.completion.text == "a reply"


@then("the router reports that no model is configured")
def no_model_configured(ctx: Ctx) -> None:
    assert isinstance(ctx.error, LLMError)
    assert "no model configured" in str(ctx.error)


@then("the router reports the provider is unavailable")
def provider_unavailable(ctx: Ctx) -> None:
    assert isinstance(ctx.error, LLMUnavailableError)


@then(parsers.parse('the reply is marked as refused with category "{category}"'))
def refused(ctx: Ctx, category: str) -> None:
    assert ctx.completion is not None
    assert ctx.completion.refused
    assert ctx.completion.refusal_category == category


@then("the next request is rejected without calling the provider")
def circuit_open(ctx: Ctx, run: Callable[[Coroutine[Any, Any, Any]], Any]) -> None:
    assert ctx.router is not None
    before = len(ctx.provider.calls)
    with pytest.raises(LLMUnavailableError):
        run(ctx.router.complete("chat", [Message(role="user", content="hi")]))
    assert len(ctx.provider.calls) == before


@then(
    parsers.parse(
        "the reply reports {input_tokens:d} input tokens and {output_tokens:d} output tokens"
    )
)
def usage_reported(ctx: Ctx, input_tokens: int, output_tokens: int) -> None:
    assert ctx.completion is not None
    assert ctx.completion.usage.input_tokens == input_tokens
    assert ctx.completion.usage.output_tokens == output_tokens


@then("the request carries no temperature")
def no_temperature(ctx: Ctx) -> None:
    assert ctx.params is not None
    assert "temperature" not in ctx.params


@then(parsers.parse("the request carries temperature {temperature:f}"))
def has_temperature(ctx: Ctx, temperature: float) -> None:
    assert ctx.params is not None
    assert ctx.params["temperature"] == pytest.approx(temperature)
