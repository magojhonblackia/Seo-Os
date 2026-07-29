"""Almacén de secretos configurables desde la UI (§ mejoras 2026-07-26).

Por qué existe: hasta ahora, configurar una API key exigía editar el archivo
.env a mano y reiniciar el servidor — una barrera dura para alguien que se
autoaloje esta app sin ser desarrollador. Este módulo agrega una capa: un
valor guardado aquí (tabla `app_settings`) GANA sobre el de .env; si no hay
override guardado, se usa el de .env exactamente como antes. Nada deja de
funcionar para quien prefiera seguir usando solo .env.

Vive en texto plano en SQLite, igual que .env vive en texto plano en disco —
mismo modelo de amenaza (regla S8: la app solo escucha en 127.0.0.1 salvo que
se configure AUTH_TOKEN explícitamente). No es una bóveda de secretos, es el
mismo nivel de protección que ya existía, con una UI encima.

La key completa NUNCA se devuelve a un GET — regla P3: un secreto que entra
por esta capa se queda en el backend, igual que uno de .env.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)

_DB_KEY_PREFIX = "secret."


# Registro de qué campos son editables desde la UI, con metadata para
# renderizar el formulario — la fuente única de verdad para routes_settings.py
# y el frontend, así agregar una key nueva es una sola entrada aquí.
SECRET_FIELDS: dict[str, dict] = {
    "deepseek_api_key": {"label": "DeepSeek (Asistente IA, resúmenes)", "category": "ia"},
    "gemini_api_key": {"label": "Google Gemini (AI Visibility)", "category": "ia"},
    "anthropic_api_key": {"label": "Anthropic Claude (AI Visibility)", "category": "ia"},
    "serper_api_key": {"label": "Serper (ranking real, Local Pack, SERP)", "category": "rankings"},
    "pagespeed_api_key": {"label": "Google PageSpeed Insights (Core Web Vitals)", "category": "technical"},
    "bing_webmaster_api_key": {"label": "Bing Webmaster (backlinks)", "category": "backlinks"},
    "indexnow_key": {"label": "IndexNow (aviso de cambios a Bing/Yandex)", "category": "technical"},
    "telegram_bot_token": {"label": "Telegram — bot token (alertas)", "category": "alertas"},
    "telegram_chat_id": {"label": "Telegram — chat id (alertas)", "category": "alertas"},
    "ga4_property_id": {"label": "Google Analytics 4 — Property ID", "category": "analytics"},
}


def _db_key(field_name: str) -> str:
    return f"{_DB_KEY_PREFIX}{field_name}"


def get_secret(field_name: str, env_value: str) -> str:
    """Valor efectivo: lo guardado desde la UI si existe, si no el de .env."""
    from backend.db.database import get_connection
    from backend.db.schema import app_settings
    from sqlalchemy import select

    with get_connection() as conn:
        row = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == _db_key(field_name))
        ).first()
    if row is not None and row[0]:
        return row[0]
    return env_value


def set_secret(field_name: str, value: str) -> None:
    if field_name not in SECRET_FIELDS:
        raise ValueError(f"'{field_name}' no es una key configurable desde la UI")
    from backend.db.database import get_connection, now_iso
    from backend.db.schema import app_settings

    with get_connection() as conn:
        stmt = sqlite_insert(app_settings).values(
            key=_db_key(field_name), value=value, updated_at=now_iso()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": now_iso()}
        )
        conn.execute(stmt)
    logger.info("Configuración: guardado override para '%s' desde la UI", field_name)


def clear_secret(field_name: str) -> None:
    """Borra el override — vuelve a usar el valor de .env (si hay alguno)."""
    from backend.db.database import get_connection
    from backend.db.schema import app_settings

    with get_connection() as conn:
        conn.execute(delete(app_settings).where(app_settings.c.key == _db_key(field_name)))
    logger.info("Configuración: eliminado override de '%s', vuelve a .env", field_name)


def list_status() -> list[dict]:
    """Estado de cada key configurable, SIN exponer el valor real — solo si
    está configurada (por .env o por la UI) y de dónde viene el valor activo."""
    from backend.config import settings
    from backend.db.database import get_connection
    from backend.db.schema import app_settings
    from sqlalchemy import select

    with get_connection() as conn:
        overridden = {
            row[0][len(_DB_KEY_PREFIX):]
            for row in conn.execute(select(app_settings.c.key)).all()
            if row[0].startswith(_DB_KEY_PREFIX)
        }

    out = []
    for field_name, meta in SECRET_FIELDS.items():
        env_value = getattr(settings, field_name, "")
        from_ui = field_name in overridden
        effective = get_secret(field_name, env_value)
        out.append({
            "field": field_name,
            "label": meta["label"],
            "category": meta["category"],
            "configured": bool(effective),
            "source": "ui" if from_ui else ("env" if env_value else None),
        })
    return out
