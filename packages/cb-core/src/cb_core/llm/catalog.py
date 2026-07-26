"""Model catalog: capabilities and pricing.

The capability flags are not cosmetic — they are what keeps the abstraction from
sending a parameter that returns a 400. The current Claude models **reject**
`temperature`, `top_p` and `top_k` outright, and reject the old
`thinking.budget_tokens` form; `effort` is accepted on some models and errors on
others. A provider-neutral layer that forwards a `temperature` it was handed
would break on exactly the models we default to, so every request is filtered
through the spec below.

Prices are USD per million tokens. Anthropic figures are from the vendored model
table (cached 2026-06-24). **OpenAI prices are deliberately `None`**: we do not
have an authoritative current figure here, and a guessed number would produce a
confidently wrong cost dashboard. Token counts are still metered; fill the values
in from the provider's pricing page to light up the USD counters.
"""

from __future__ import annotations

from typing import Literal

import msgspec

ThinkingStyle = Literal["adaptive", "budget", "none"]


class ModelSpec(msgspec.Struct, frozen=True):
    provider: str
    model_id: str
    context_window: int
    max_output: int
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    # Current Claude models return 400 for temperature/top_p/top_k.
    supports_sampling: bool = True
    # `output_config.effort` — accepted on Claude 4.6+, errors on Haiku 4.5.
    supports_effort: bool = False
    thinking: ThinkingStyle = "none"
    # Thinking is on unless explicitly disabled (Claude Opus 5, Sonnet 5).
    thinking_on_by_default: bool = False
    # Server-side refusal fallback (Claude API only).
    supports_fallbacks: bool = False
    # Above this, non-streaming requests risk an SDK HTTP timeout.
    stream_above_max_tokens: int = 16_000

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.input_usd_per_mtok is None or self.output_usd_per_mtok is None:
            return None
        return (
            input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok
        ) / 1_000_000


CATALOG: dict[str, ModelSpec] = {
    # ---- Anthropic ----
    "claude-opus-5": ModelSpec(
        provider="anthropic",
        model_id="claude-opus-5",
        context_window=1_000_000,
        max_output=128_000,
        input_usd_per_mtok=5.00,
        output_usd_per_mtok=25.00,
        supports_sampling=False,
        supports_effort=True,
        thinking="adaptive",
        thinking_on_by_default=True,
        supports_fallbacks=True,
    ),
    "claude-sonnet-5": ModelSpec(
        provider="anthropic",
        model_id="claude-sonnet-5",
        context_window=1_000_000,
        max_output=128_000,
        input_usd_per_mtok=3.00,
        output_usd_per_mtok=15.00,
        supports_sampling=False,
        supports_effort=True,
        thinking="adaptive",
        thinking_on_by_default=True,
    ),
    "claude-haiku-4-5": ModelSpec(
        provider="anthropic",
        model_id="claude-haiku-4-5",
        context_window=200_000,
        max_output=64_000,
        input_usd_per_mtok=1.00,
        output_usd_per_mtok=5.00,
        # Haiku 4.5 predates the effort ladder and still accepts sampling params.
        supports_sampling=True,
        supports_effort=False,
        thinking="budget",
    ),
    # ---- OpenAI / OpenAI-compatible (ollama, openrouter, vLLM, …) ----
    # Pricing intentionally unset — see module docstring.
    "gpt-5": ModelSpec(
        provider="openai",
        model_id="gpt-5",
        context_window=400_000,
        max_output=128_000,
        supports_sampling=True,
    ),
    "gpt-5-mini": ModelSpec(
        provider="openai",
        model_id="gpt-5-mini",
        context_window=400_000,
        max_output=128_000,
        supports_sampling=True,
    ),
    "whisper-1": ModelSpec(
        provider="openai",
        model_id="whisper-1",
        context_window=0,
        max_output=0,
        supports_sampling=False,
    ),
}


def spec_for(model_id: str, *, provider: str | None = None) -> ModelSpec:
    """Look up a model, falling back to a conservative unknown-model spec.

    An unlisted model is not an error — operators must be able to point the bot
    at a new model the day it ships, or at a self-hosted one, without waiting for
    a code change. The fallback disables every optional parameter so the request
    stays valid on the widest set of backends, and leaves pricing unknown so the
    cost counters stay honest rather than wrong.
    """
    known = CATALOG.get(model_id)
    if known is not None and (provider is None or known.provider == provider):
        return known
    return ModelSpec(
        provider=provider or "openai",
        model_id=model_id,
        context_window=128_000,
        max_output=8_192,
        supports_sampling=False,
        supports_effort=False,
        thinking="none",
    )


def register(spec: ModelSpec) -> None:
    """Add or override a catalog entry at runtime (operator config, tests)."""
    CATALOG[spec.model_id] = spec
