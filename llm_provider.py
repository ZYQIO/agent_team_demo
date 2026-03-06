#!/usr/bin/env python3
"""
Backward-compatible provider module.

Prefer importing model adapters from `agent_team.models`.
"""

from agent_team.models import (  # noqa: F401
    HeuristicProvider,
    LLMProvider,
    OpenAICompatibleProvider,
    ProviderMetadata,
    build_provider,
)

__all__ = [
    "HeuristicProvider",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "ProviderMetadata",
    "build_provider",
]
