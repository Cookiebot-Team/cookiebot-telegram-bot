"""Provider contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from cb_core.llm.types import Completion, Message, Transcript


@runtime_checkable
class LLMProvider(Protocol):
    """One vendor's chat/transcription surface.

    Implementations own retries, parameter filtering and usage extraction; the
    router owns model selection, metering and circuit breaking.
    """

    @property
    def name(self) -> str: ...

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        effort: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
    ) -> Completion: ...

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        effort: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas. The final Completion is available via `last_completion`."""
        yield ""

    async def count_tokens(
        self, messages: Sequence[Message], *, model: str, system: str | None = None
    ) -> int:
        """Exact input token count for this model.

        Providers that expose a counting endpoint must use it. Estimating with a
        different vendor's tokenizer is wrong by 15-30% and silently corrupts any
        budget built on it.
        """
        ...

    async def transcribe(
        self, audio: bytes, *, model: str, filename: str = "audio.ogg", language: str | None = None
    ) -> Transcript: ...

    async def close(self) -> None: ...
