"""Multi-provider LLM access with configurable per-task models."""

from __future__ import annotations

from cb_core.llm.base import LLMProvider
from cb_core.llm.catalog import CATALOG, ModelSpec, register, spec_for
from cb_core.llm.router import (
    DEFAULT_TASKS,
    LLMRouter,
    TaskConfig,
    build_router,
    close_llm,
    init_llm,
    router,
)
from cb_core.llm.types import (
    Completion,
    LLMError,
    LLMRateLimitedError,
    LLMUnavailableError,
    Message,
    Transcript,
    Usage,
)

__all__ = [
    "CATALOG",
    "DEFAULT_TASKS",
    "Completion",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitedError",
    "LLMRouter",
    "LLMUnavailableError",
    "Message",
    "ModelSpec",
    "TaskConfig",
    "Transcript",
    "Usage",
    "build_router",
    "close_llm",
    "init_llm",
    "register",
    "router",
    "spec_for",
]
