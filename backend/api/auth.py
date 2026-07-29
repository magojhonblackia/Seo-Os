"""Auth opcional por Bearer token (§4.2, S8 del PROMPT_MAESTRO).

Fase 0-3: sin auth, la app solo escucha en 127.0.0.1 — sin superficie de
ataque de red. Si se necesita exponer en LAN (HOST != 127.0.0.1), config.py
ya exige un AUTH_TOKEN configurado antes de arrancar; este módulo es el que
lo hace cumplir en cada request a la API.

Alcance honesto: esto protege las rutas /api/* (montadas con este dependency).
El HTML/JS estático (`frontend/`) se sirve sin este check porque StaticFiles
es una sub-app ASGI aparte, fuera del sistema de dependencias de FastAPI — o
sea, alguien en la misma red podría ver el shell de la app, pero no podría
leer ni modificar datos sin el token correcto. No es protección de nivel
empresarial (P: "no sobre-ingeniería" para Fase 4) — es la barrera mínima
razonable para no dejar la API abierta en una LAN.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from backend.config import settings


def require_auth_if_configured(authorization: str | None = Header(default=None)) -> None:
    if not settings.has_auth_token:
        return  # sin AUTH_TOKEN configurado: comportamiento de siempre, sin auth (uso local)

    expected = f"Bearer {settings.auth_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Token inválido o faltante (Authorization: Bearer <token>)")
