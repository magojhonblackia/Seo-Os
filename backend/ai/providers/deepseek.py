"""Implementación DeepSeek (API compatible con formato OpenAI).

Nunca loguea la API key (el filtro de redacción de backend/config.py ya la
protege en logs, pero además aquí no se imprime nunca el header Authorization).
"""
from __future__ import annotations

import logging

import httpx

from backend.ai.engine import AIError, AIResponse, Message

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2

# Precios aproximados por 1M tokens (verificar en platform.deepseek.com antes
# de confiar en esto para facturación real — es una ESTIMACIÓN para que el
# usuario tenga una idea de costo, regla P6, no un cobro exacto).
PRICE_PER_1M_INPUT_USD = 0.27
PRICE_PER_1M_OUTPUT_USD = 1.10


class DeepSeekProvider:
    def __init__(self, api_key: str):
        self._api_key = api_key

    async def chat(
        self, messages: list[Message], *, max_tokens: int = 1500, temperature: float = 0.4
    ) -> AIResponse:
        payload = {
            "model": MODEL,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        data = None
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.post(DEEPSEEK_URL, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    break
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in (401, 403):
                        raise AIError(
                            "DeepSeek rechazó la API key configurada (401/403). Verifica DEEPSEEK_API_KEY en .env."
                        ) from exc
                    if attempt == MAX_RETRIES:
                        raise AIError(f"DeepSeek respondió error HTTP {exc.response.status_code}") from exc
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt == MAX_RETRIES:
                        raise AIError(f"No se pudo conectar a DeepSeek: {exc}") from exc

        if data is None:
            raise AIError(f"DeepSeek falló tras {MAX_RETRIES} reintentos: {last_error}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIError("Respuesta de DeepSeek con formato inesperado") from exc

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

        cost = (
            (input_tokens / 1_000_000) * PRICE_PER_1M_INPUT_USD
            + (output_tokens / 1_000_000) * PRICE_PER_1M_OUTPUT_USD
        )

        return AIResponse(content=content, tokens_used=total_tokens, cost_estimate=round(cost, 6), model=MODEL)
