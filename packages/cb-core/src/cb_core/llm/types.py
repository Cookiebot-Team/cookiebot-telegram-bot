"""Provider-neutral LLM types.

Deliberately small: text in, text out, plus the usage and refusal signals we need
for cost metering and moderation. Anything provider-specific (thinking blocks,
tool schemas, fallback routing) is handled inside a provider and does not leak
into handler code.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

Role = Literal["system", "user", "assistant"]


class Message(msgspec.Struct, frozen=True):
    role: Role
    content: str


class Usage(msgspec.Struct, frozen=True):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


StopReason = Literal["end_turn", "max_tokens", "stop_sequence", "tool_use", "refusal", "other"]


class Completion(msgspec.Struct, frozen=True):
    """One model response.

    `stop_reason == "refusal"` means the provider's safety classifiers declined.
    That is a normal, successful HTTP response — callers must branch on it before
    using `text`, which will be empty or partial.
    """

    text: str
    model: str
    provider: str
    usage: Usage
    stop_reason: StopReason = "end_turn"
    refusal_category: str | None = None
    cost_usd: float | None = None
    raw: Any = None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


class Transcript(msgspec.Struct, frozen=True):
    """Speech-to-text result (core_musicdetection / voice handling)."""

    text: str
    model: str
    provider: str
    language: str | None = None
    duration_seconds: float | None = None
    cost_usd: float | None = None


class LLMError(RuntimeError):
    """Provider call failed after retries."""


class LLMRateLimitedError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMUnavailableError(LLMError):
    """Circuit open, or the provider is not configured."""


class LLMBudgetExceededError(LLMError):
    """Tenant's monthly LLM spend cap is exceeded (`Tenant.monthly_llm_budget_usd`).

    Not the same failure class as `LLMUnavailableError`: this is a business-rule
    refusal from a spend query that *succeeded*, not an infrastructure problem.
    A cache or database failure while computing the spend fails open instead of
    raising this — see `llm/budget.py`'s `ensure_within_budget`.
    """

    def __init__(self, tenant_id: str, spent_usd: float, budget_usd: float) -> None:
        super().__init__(
            f"tenant {tenant_id!r} is over its monthly LLM budget "
            f"(${spent_usd:.2f} spent of ${budget_usd:.2f})"
        )
        self.tenant_id = tenant_id
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
