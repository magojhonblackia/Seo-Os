"""API de la pantalla de Configuración (§ mejoras 2026-07-26).

Existe para que alguien que se autoaloje esta app pueda pegar sus API keys
desde una pantalla, en vez de editar .env a mano y reiniciar el servidor.

Regla P3, sin excepción aquí tampoco: la key completa NUNCA sale en un GET —
solo si está configurada y de dónde viene el valor activo (.env o guardado
por esta misma pantalla). Guardar es un POST de solo escritura.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.settings_store import SECRET_FIELDS, clear_secret, list_status, set_secret

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SetSecretRequest(BaseModel):
    value: str = Field(min_length=1, max_length=2000)


@router.get("")
def get_settings_status() -> dict:
    return {"fields": list_status()}


@router.post("/{field_name}")
def save_secret(field_name: str, payload: SetSecretRequest) -> dict:
    if field_name not in SECRET_FIELDS:
        raise HTTPException(status_code=404, detail=f"'{field_name}' no es una key configurable")
    set_secret(field_name, payload.value.strip())
    return {"field": field_name, "saved": True}


@router.delete("/{field_name}")
def revert_secret(field_name: str) -> dict:
    """Borra el valor guardado desde la UI — la app vuelve a usar .env."""
    if field_name not in SECRET_FIELDS:
        raise HTTPException(status_code=404, detail=f"'{field_name}' no es una key configurable")
    clear_secret(field_name)
    return {"field": field_name, "reverted": True}
