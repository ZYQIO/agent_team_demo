#!/usr/bin/env python3
"""
LLM provider abstraction for agent team runtime.

Providers:
- heuristic: local deterministic fallback, no external API
- openai: OpenAI-compatible chat completions endpoint
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.error
import urllib.request
from typing import Tuple


@dataclasses.dataclass
class ProviderMetadata:
    provider: str
    model: str
    mode: str
    note: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class LLMProvider:
    def __init__(self, metadata: ProviderMetadata) -> None:
        self.metadata = metadata

    def complete(self, system_prompt: str, user_prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class HeuristicProvider(LLMProvider):
    def __init__(self, model: str = "heuristic-v1", note: str = "") -> None:
        super().__init__(
            metadata=ProviderMetadata(
                provider="heuristic",
                model=model,
                mode="local",
                note=note,
            )
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        del system_prompt
        compressed = " ".join(user_prompt.strip().split())
        if len(compressed) > 700:
            compressed = compressed[:700] + "..."
        return (
            "Priority actions:\n"
            "1. Fix missing top-level headings first to improve navigation.\n"
            "2. Split oversized files into focused sections with clear TOC.\n"
            "3. Add markdown lint checks in CI.\n\n"
            f"Context snapshot: {compressed}"
        )


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_sec: int = 60,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for OpenAI provider")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        super().__init__(
            metadata=ProviderMetadata(
                provider="openai",
                model=model,
                mode="remote",
                note=f"endpoint={self.base_url}",
            )
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - depends on external API
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"openai http error: status={exc.code} body={body[:500]}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - depends on network
            raise RuntimeError(f"openai network error: {exc}") from exc

        choices = data.get("choices")
        if not choices:
            raise RuntimeError("openai response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("openai response missing text content")
        return content.strip()


def build_provider(
    provider_name: str,
    model: str,
    openai_api_key_env: str,
    openai_base_url: str,
    require_llm: bool,
    timeout_sec: int = 60,
) -> Tuple[LLMProvider, ProviderMetadata]:
    normalized = provider_name.strip().lower()
    if normalized == "heuristic":
        provider = HeuristicProvider(model=model or "heuristic-v1")
        return provider, provider.metadata

    if normalized != "openai":
        raise ValueError(f"unsupported provider: {provider_name}")

    api_key = os.getenv(openai_api_key_env, "").strip()
    if not api_key:
        if require_llm:
            raise RuntimeError(
                f"provider=openai requires env var {openai_api_key_env} but it is missing"
            )
        provider = HeuristicProvider(
            model="heuristic-fallback",
            note=f"fallback because {openai_api_key_env} is missing",
        )
        return provider, provider.metadata

    provider = OpenAICompatibleProvider(
        api_key=api_key,
        base_url=openai_base_url,
        model=model or "gpt-4.1-mini",
        timeout_sec=timeout_sec,
    )
    return provider, provider.metadata
