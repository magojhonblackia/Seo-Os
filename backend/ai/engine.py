"""Abstracción de proveedor LLM (§7.1 del PROMPT_MAESTRO).

Cambiar de proveedor = agregar un archivo en ai/providers/ + un branch en
get_provider(). La API key NUNCA llega al frontend (regla P3): toda llamada
a un LLM pasa por aquí, en el backend, nunca desde JS del navegador.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class AIResponse:
    content: str
    tokens_used: int
    cost_estimate: float
    model: str


class LLMProvider(Protocol):
    async def chat(
        self, messages: list[Message], *, max_tokens: int = 1500, temperature: float = 0.4
    ) -> AIResponse: ...


class AIError(Exception):
    """Error de proveedor LLM. Mensaje siempre legible para mostrar al usuario
    (regla S3: degradarse con gracia, nunca un stacktrace crudo)."""


def get_provider() -> LLMProvider:
    from backend.config import settings

    if not settings.has_deepseek:
        raise AIError(
            "DeepSeek no está configurado: agrega DEEPSEEK_API_KEY a tu archivo .env "
            "(ver README.md, sección Fase 2) y reinicia el servidor."
        )

    from backend.ai.providers.deepseek import DeepSeekProvider

    from backend.settings_store import get_secret

    return DeepSeekProvider(api_key=get_secret("deepseek_api_key", settings.deepseek_api_key))
