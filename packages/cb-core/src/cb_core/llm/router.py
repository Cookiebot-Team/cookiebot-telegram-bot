"""Task-based routing, metering and failure isolation.

Handlers ask for a *task* (`chat`, `moderate`, `summarize`, `transcribe`), never
a model. Which model serves a task is configuration, so switching the chat model
to Sonnet, or pointing `transcribe` at a self-hosted endpoint, is an env change
rather than a code change.

Everything a call costs is recorded: Prometheus counters for live dashboards and
a per-group `llm_usage` row in Citus for attribution. v1 called OpenAI from three
places with no accounting at all, so nobody could answer "which group is spending
the money".
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import msgspec

from cb_core import metrics
from cb_core.breaker import Breaker
from cb_core.ids import uuid7
from cb_core.llm.base import LLMProvider
from cb_core.llm.catalog import spec_for
from cb_core.llm.types import (
    Completion,
    LLMError,
    LLMUnavailableError,
    Message,
    Transcript,
)
from cb_core.logging import get_logger
from cb_core.settings import Settings
from cb_core.telemetry import current_trace_id, span

log = get_logger("cb.llm")


class TaskConfig(msgspec.Struct, frozen=True):
    provider: str
    model: str
    max_tokens: int = 1024
    effort: str | None = None
    thinking: bool | None = None
    temperature: float | None = None
    timeout: float | None = 60.0
    system: str | None = None


# Conservative defaults. `chat` runs behind the langchain provider so a task can
# name any "provider:model" string (R1.8) — `effort` is dropped for it, since
# there is no portable effort parameter across vendors and carrying one that
# only works for a single backend would defeat the point of the abstraction.
# `moderate`/`summarize`/`vision`/`transcribe` stay on the hand-rolled providers,
# so `doomlist`'s live `moderate` calls are untouched by this move.
DEFAULT_TASKS: dict[str, TaskConfig] = {
    "chat": TaskConfig(
        provider="langchain",
        model="anthropic:claude-opus-5",
        max_tokens=1024,
        temperature=1.0,
        timeout=30.0,
    ),
    "moderate": TaskConfig(
        provider="anthropic", model="claude-haiku-4-5", max_tokens=256, temperature=0.0
    ),
    "summarize": TaskConfig(
        provider="anthropic", model="claude-sonnet-5", max_tokens=2048, effort="low"
    ),
    "vision": TaskConfig(
        provider="anthropic", model="claude-sonnet-5", max_tokens=1024, effort="medium"
    ),
    "transcribe": TaskConfig(provider="openai", model="whisper-1", max_tokens=0),
}


# The breaker moved to `cb_core.breaker` when the doomlist port needed the same
# semantics for cas.chat and burrbot. `_Breaker` stays as the local name.
_Breaker = Breaker


class LLMRouter:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        tasks: dict[str, TaskConfig] | None = None,
        *,
        record_usage: bool = True,
    ) -> None:
        self._providers = providers
        self._tasks = {**DEFAULT_TASKS, **(tasks or {})}
        self._breakers = {name: _Breaker() for name in providers}
        self._record_usage = record_usage

    def config_for(self, task: str) -> TaskConfig:
        try:
            return self._tasks[task]
        except KeyError:
            raise LLMError(f"no model configured for task {task!r}") from None

    def provider_for(self, task: str) -> LLMProvider:
        cfg = self.config_for(task)
        provider = self._providers.get(cfg.provider)
        if provider is None:
            raise LLMUnavailableError(f"provider {cfg.provider!r} is not configured")
        return provider

    async def complete(
        self,
        task: str,
        messages: Sequence[Message],
        *,
        group_id: int | None = None,
        user_id: int | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        tenant_id: str | None = None,
    ) -> Completion:
        cfg = self.config_for(task)
        provider = self.provider_for(task)
        breaker = self._breakers[cfg.provider]
        now = time.monotonic()

        if tenant_id is not None:
            # R2.5: `tenant_id=None` (every caller before this task) skips the
            # check entirely. A budget refusal is not a provider failure, so it
            # runs before the breaker gate and outside its accounting.
            from cb_core.llm.budget import ensure_within_budget

            await ensure_within_budget(tenant_id)

        if not breaker.allow(now):
            metrics.llm_requests_total.labels(
                provider=cfg.provider, model=cfg.model, task=task, outcome="circuit_open"
            ).inc()
            raise LLMUnavailableError(f"{cfg.provider} circuit is open")

        start = time.perf_counter()
        outcome = "ok"
        completion: Completion | None = None
        with span(
            f"llm.{task}",
            # mypy conflates this **dict-unpack with `span`'s `kind` keyword param
            # since it cannot see that none of these dotted keys is literally "kind".
            **{"llm.provider": cfg.provider, "llm.model": cfg.model, "llm.task": task},  # type: ignore[arg-type]
        ) as sp:
            try:
                completion = await provider.complete(
                    messages,
                    model=cfg.model,
                    max_tokens=max_tokens or cfg.max_tokens,
                    system=system or cfg.system,
                    temperature=cfg.temperature,
                    effort=cfg.effort,
                    thinking=cfg.thinking,
                    timeout=cfg.timeout,
                )
            except Exception:
                outcome = "error"
                breaker.record(False, now)
                raise
            finally:
                elapsed = time.perf_counter() - start
                if completion is not None and completion.refused:
                    outcome = "refusal"
                metrics.llm_duration.labels(
                    provider=cfg.provider, model=cfg.model, task=task, outcome=outcome
                ).observe(elapsed)
                metrics.llm_requests_total.labels(
                    provider=cfg.provider, model=cfg.model, task=task, outcome=outcome
                ).inc()
                sp.set_attribute("llm.outcome", outcome)

        breaker.record(True, now)

        if completion.refused:
            metrics.llm_refusals_total.labels(
                provider=completion.provider,
                model=completion.model,
                category=completion.refusal_category or "unknown",
            ).inc()
            log.info(
                "llm.refused",
                task=task,
                model=completion.model,
                category=completion.refusal_category,
            )

        self._meter(task, completion)
        if self._record_usage and group_id is not None:
            await self._persist(task, completion, group_id, user_id, time.perf_counter() - start)
        return completion

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.ogg",
        language: str | None = None,
        group_id: int | None = None,
        tenant_id: str | None = None,
    ) -> Transcript:
        cfg = self.config_for("transcribe")
        provider = self.provider_for("transcribe")

        if tenant_id is not None:
            from cb_core.llm.budget import ensure_within_budget

            await ensure_within_budget(tenant_id)

        start = time.perf_counter()
        outcome = "ok"
        try:
            with span(
                "llm.transcribe",
                **{"llm.provider": cfg.provider, "llm.model": cfg.model},  # type: ignore[arg-type]  # see complete()
            ):
                return await provider.transcribe(
                    audio, model=cfg.model, filename=filename, language=language
                )
        except Exception:
            outcome = "error"
            raise
        finally:
            metrics.llm_duration.labels(
                provider=cfg.provider, model=cfg.model, task="transcribe", outcome=outcome
            ).observe(time.perf_counter() - start)
            metrics.llm_requests_total.labels(
                provider=cfg.provider, model=cfg.model, task="transcribe", outcome=outcome
            ).inc()

    async def count_tokens(
        self, task: str, messages: Sequence[Message], *, system: str | None = None
    ) -> int:
        cfg = self.config_for(task)
        return await self.provider_for(task).count_tokens(
            messages, model=cfg.model, system=system or cfg.system
        )

    def fits_context(self, task: str, token_count: int, *, headroom: int = 4096) -> bool:
        cfg = self.config_for(task)
        spec = spec_for(cfg.model, provider=cfg.provider)
        return token_count + cfg.max_tokens + headroom <= spec.context_window

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()

    # ---------------------------------------------------------------- accounting

    @staticmethod
    def _meter(task: str, completion: Completion) -> None:
        labels = {"provider": completion.provider, "model": completion.model}
        metrics.llm_tokens_total.labels(**labels, kind="input").inc(completion.usage.input_tokens)
        metrics.llm_tokens_total.labels(**labels, kind="output").inc(completion.usage.output_tokens)
        if completion.usage.cache_read_tokens:
            metrics.llm_tokens_total.labels(**labels, kind="cache_read").inc(
                completion.usage.cache_read_tokens
            )
        if completion.cost_usd is not None:
            metrics.llm_cost_usd_total.labels(**labels).inc(completion.cost_usd)

    @staticmethod
    async def _persist(
        task: str,
        completion: Completion,
        group_id: int,
        user_id: int | None,
        latency: float,
    ) -> None:
        """One `llm_usage` row per call, on the group's shard.

        Never raises into a handler — losing an accounting row must not cost a reply.
        """
        from cb_core import db

        try:
            await db.execute(
                """
                INSERT INTO llm_usage (
                    usage_id, group_id, user_id, task, provider, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cost_usd, latency_ms, outcome, trace_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                uuid7(),
                group_id,
                user_id,
                task,
                completion.provider,
                completion.model,
                completion.usage.input_tokens,
                completion.usage.output_tokens,
                completion.usage.cache_read_tokens,
                completion.cost_usd,
                int(latency * 1000),
                "refusal" if completion.refused else "ok",
                current_trace_id(),
                name="llm_usage_insert",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.usage_persist_failed", error=str(exc), task=task)


# ---------------------------------------------------------------------- assembly

_router: LLMRouter | None = None


def build_router(settings: Settings) -> LLMRouter:
    """Construct providers from settings. Absent credentials disable a provider
    rather than crashing the service — a bot with no OpenAI key should still boot,
    and only `transcribe` should fail."""
    from cb_core.llm.anthropic_provider import AnthropicProvider
    from cb_core.llm.langchain_provider import LangchainProvider
    from cb_core.llm.openai_provider import OpenAIProvider

    providers: dict[str, LLMProvider] = {}
    if settings.anthropic_api_key or settings.llm_allow_ambient_credentials:
        providers["anthropic"] = AnthropicProvider(
            settings.anthropic_api_key or None,
            timeout=settings.llm_timeout_seconds,
            refusal_fallback=settings.llm_refusal_fallback,
        )
    if settings.openai_api_key or settings.openai_base_url:
        providers["openai"] = OpenAIProvider(
            settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            timeout=settings.llm_timeout_seconds,
        )
    # Registered unconditionally: credential resolution happens inside each
    # per-model integration package, so there is nothing to gate on at boot. An
    # unconfigured model surfaces as an `LLMError` at call time (R1.7).
    providers["langchain"] = LangchainProvider(settings)

    tasks = {name: msgspec.convert(cfg, TaskConfig) for name, cfg in settings.llm_tasks.items()}
    log.info(
        "llm.router.ready", providers=sorted(providers), tasks=sorted({**DEFAULT_TASKS, **tasks})
    )
    return LLMRouter(providers, tasks)


def init_llm(settings: Settings) -> LLMRouter:
    global _router
    if _router is None:
        _router = build_router(settings)
    return _router


def router() -> LLMRouter:
    if _router is None:
        raise RuntimeError("llm router not initialised; call init_llm() during startup")
    return _router


async def close_llm() -> None:
    global _router
    if _router is not None:
        await _router.close()
        _router = None
