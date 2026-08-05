"""Unit tests for the langchain-backed provider.

`init_chat_model` is monkeypatched to a fake client rather than exercised for
real: it would otherwise reach out to whichever vendor SDK it resolves to. The
fake stands in for a `BaseChatModel` and only needs the three methods the
provider actually calls (`ainvoke`, `astream`, `get_num_tokens_from_messages`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from cb_core.llm import langchain_provider
from cb_core.llm.catalog import CATALOG
from cb_core.llm.langchain_provider import LangchainProvider
from cb_core.llm.router import DEFAULT_TASKS
from cb_core.llm.types import LLMError, Message
from cb_core.settings import Settings


class _FakeClient:
    """Stands in for a resolved `BaseChatModel`; records what it was sent."""

    def __init__(
        self,
        message: AIMessage,
        *,
        chunks: Sequence[AIMessageChunk] = (),
        token_count: int = 0,
    ) -> None:
        self.message = message
        self.chunks = chunks
        self.token_count = token_count
        self.calls: list[Sequence[BaseMessage]] = []

    async def ainvoke(self, messages: Sequence[BaseMessage], **kwargs: Any) -> AIMessage:
        self.calls.append(messages)
        return self.message

    async def astream(
        self, messages: Sequence[BaseMessage], **kwargs: Any
    ) -> AsyncIterator[AIMessageChunk]:
        self.calls.append(messages)
        for chunk in self.chunks:
            yield chunk

    def get_num_tokens_from_messages(
        self, messages: Sequence[BaseMessage], tools: Any = None
    ) -> int:
        self.calls.append(messages)
        return self.token_count


def _install_fake(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> list[dict[str, Any]]:
    """Patch `init_chat_model` to return `client`; returns the list of resolve calls."""
    calls: list[dict[str, Any]] = []

    def fake_init_chat_model(model: str, **kwargs: Any) -> _FakeClient:
        calls.append({"model": model, **kwargs})
        return client

    monkeypatch.setattr(langchain_provider, "init_chat_model", fake_init_chat_model)
    return calls


@pytest.fixture
def provider() -> LangchainProvider:
    return LangchainProvider(Settings())


class TestName:
    def test_name_is_langchain(self, provider: LangchainProvider) -> None:
        assert provider.name == "langchain"


class TestComplete:
    async def test_maps_roles_and_builds_completion(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = AIMessage(
            content="hi there",
            usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            response_metadata={"stop_reason": "end_turn"},
        )
        client = _FakeClient(message)
        _install_fake(monkeypatch, client)

        result = await provider.complete(
            [
                Message(role="assistant", content="earlier reply"),
                Message(role="user", content="hello"),
            ],
            model="anthropic:claude-opus-5",
            max_tokens=100,
            system="be terse",
        )

        assert result.text == "hi there"
        assert result.provider == "langchain"
        assert result.model == "anthropic:claude-opus-5"
        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 50
        assert result.stop_reason == "end_turn"

        sent = client.calls[0]
        assert [type(m) for m in sent] == [SystemMessage, AIMessage, HumanMessage]
        assert sent[0].content == "be terse"
        assert sent[1].content == "earlier reply"
        assert sent[2].content == "hello"

    async def test_cache_read_tokens_from_input_token_details(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 40},
            },
        )
        _install_fake(monkeypatch, _FakeClient(message))

        result = await provider.complete(
            [Message(role="user", content="hi")], model="anthropic:claude-opus-5", max_tokens=10
        )
        assert result.usage.cache_read_tokens == 40

    async def test_finish_reason_vocabulary_is_normalised(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenAI-flavoured langchain integrations populate `finish_reason`, not
        `stop_reason` — both must map onto the same `StopReason` values the
        hand-rolled providers use."""
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            response_metadata={"finish_reason": "length"},
        )
        _install_fake(monkeypatch, _FakeClient(message))

        result = await provider.complete(
            [Message(role="user", content="hi")], model="openai:gpt-5", max_tokens=10
        )
        assert result.stop_reason == "max_tokens"

    async def test_client_is_cached_per_config(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        resolve_calls = _install_fake(monkeypatch, _FakeClient(message))

        for _ in range(3):
            await provider.complete(
                [Message(role="user", content="hi")],
                model="anthropic:claude-opus-5",
                max_tokens=100,
                temperature=1.0,
                timeout=30.0,
            )
        assert len(resolve_calls) == 1

        # A different config is a cache miss.
        await provider.complete(
            [Message(role="user", content="hi")],
            model="anthropic:claude-opus-5",
            max_tokens=200,
            temperature=1.0,
            timeout=30.0,
        )
        assert len(resolve_calls) == 2

    async def test_cost_lookup_strips_the_provider_prefix(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`"anthropic:claude-opus-5"` must price exactly as `"claude-opus-5"`
        does today — the catalog is keyed on the bare model id."""
        message = AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
        )
        _install_fake(monkeypatch, _FakeClient(message))

        result = await provider.complete(
            [Message(role="user", content="hi")], model="anthropic:claude-opus-5", max_tokens=10
        )
        expected = CATALOG["claude-opus-5"].cost_usd(1_000_000, 1_000_000)
        assert expected is not None
        assert result.cost_usd == pytest.approx(expected)

    async def test_unpriced_model_reports_none_not_a_guess(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
        )
        _install_fake(monkeypatch, _FakeClient(message))

        result = await provider.complete(
            [Message(role="user", content="hi")], model="openai:gpt-4o-mini", max_tokens=10
        )
        assert result.cost_usd is None


class TestTemperatureGating:
    """Would have caught the original bug: `_resolve` forwarded `temperature`
    to `init_chat_model` unconditionally, so `DEFAULT_TASKS["chat"]`'s
    `temperature=1.0` reached `claude-opus-5` (`supports_sampling=False`) and
    every chat reply 400'd."""

    async def test_shipped_chat_default_drops_temperature_for_the_model_that_rejects_it(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = DEFAULT_TASKS["chat"]
        # Sanity: this test only proves something if the shipped config still
        # carries a temperature, and the shipped model still can't take one.
        assert cfg.temperature is not None
        bare_model = cfg.model.partition(":")[2]
        assert CATALOG[bare_model].supports_sampling is False

        message = AIMessage(
            content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )
        calls = _install_fake(monkeypatch, _FakeClient(message))

        await provider.complete(
            [Message(role="user", content="hi")],
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            timeout=cfg.timeout,
        )

        assert "temperature" not in calls[0], calls[0]

    async def test_temperature_is_forwarded_for_a_model_that_supports_it(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert CATALOG["claude-haiku-4-5"].supports_sampling is True

        message = AIMessage(
            content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )
        calls = _install_fake(monkeypatch, _FakeClient(message))

        await provider.complete(
            [Message(role="user", content="hi")],
            model="anthropic:claude-haiku-4-5",
            max_tokens=100,
            temperature=0.5,
        )

        assert calls[0]["temperature"] == 0.5


class TestStream:
    async def test_yields_chunk_text(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(
            AIMessage(content=""),
            chunks=[AIMessageChunk(content="Hel"), AIMessageChunk(content="lo")],
        )
        _install_fake(monkeypatch, client)

        chunks = [
            chunk
            async for chunk in provider.stream(
                [Message(role="user", content="hi")], model="anthropic:claude-opus-5", max_tokens=10
            )
        ]
        assert chunks == ["Hel", "lo"]


class TestCountTokens:
    async def test_delegates_to_the_client(
        self, provider: LangchainProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient(AIMessage(content=""), token_count=42)
        _install_fake(monkeypatch, client)

        count = await provider.count_tokens(
            [Message(role="user", content="hi")], model="anthropic:claude-opus-5", system="rules"
        )
        assert count == 42
        sent = client.calls[0]
        assert [type(m) for m in sent] == [SystemMessage, HumanMessage]


class TestTranscribe:
    async def test_raises_and_routes_to_openai(self, provider: LangchainProvider) -> None:
        with pytest.raises(LLMError, match="route to openai"):
            await provider.transcribe(b"audio bytes", model="whisper-1")


class TestClose:
    async def test_is_a_noop(self, provider: LangchainProvider) -> None:
        await provider.close()
