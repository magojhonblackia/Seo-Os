"""Único punto de acceso a secretos y configuración (regla P2, S1 del PROMPT_MAESTRO).

Ningún otro módulo debe llamar a os.getenv directamente: todos importan `settings`
desde aquí. Si falta una key opcional, el módulo que la necesita se desactiva
con un mensaje claro (S3) en lugar de fallar toda la aplicación.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "data/seo.db"

    deepseek_api_key: str = ""
    google_application_credentials: str = "credentials/gsc-service-account.json"
    pagespeed_api_key: str = ""

    bing_webmaster_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    serper_api_key: str = ""

    # AI Visibility (§ mejoras 2026-07-26): consultar directamente a los
    # modelos de IA públicos qué dicen sobre el negocio — distinto del GEO
    # score (que mide si los CRAWLERS de IA pueden acceder al sitio). Requiere
    # gasto real por request en cada proveedor: por eso nunca corre solo, es
    # disparo manual explícito (mismo criterio que rank_tracking/IndexNow).
    gemini_api_key: str = ""
    anthropic_api_key: str = ""

    # ID numérico de la propiedad GA4 (no el "G-XXXX" de medición). Se puede
    # sobrescribir por proyecto en projects.config["ga4_property_id"] cuando se
    # audita más de un sitio. Usa el MISMO service account que Search Console:
    # solo hay que darle acceso de lectura a la propiedad en GA4.
    ga4_property_id: str = ""

    # IndexNow (§ herramientas de mercado 2026-07-24): protocolo abierto que
    # notifica a Bing/Yandex/Seznam/Naver que una URL cambió, sin esperar a que
    # vuelvan a crawlear por su cuenta. Complementa GSC (que solo habla con
    # Google). La key es cualquier string alfanumérico que TÚ generas y alojas
    # en https://tu-sitio.com/<key>.txt con ese mismo valor como contenido —
    # ver .env.example para el paso a paso.
    indexnow_key: str = ""

    auth_token: str = ""

    @property
    def db_full_path(self) -> Path:
        return BASE_DIR / self.database_path

    @property
    def has_deepseek(self) -> bool:
        return bool(_resolved("deepseek_api_key", self.deepseek_api_key))

    @property
    def gsc_credentials_path(self) -> Path:
        return BASE_DIR / self.google_application_credentials

    @property
    def has_gsc_credentials(self) -> bool:
        return self.gsc_credentials_path.exists()

    @property
    def has_pagespeed(self) -> bool:
        return bool(_resolved("pagespeed_api_key", self.pagespeed_api_key))

    @property
    def has_bing_webmaster(self) -> bool:
        return bool(_resolved("bing_webmaster_api_key", self.bing_webmaster_api_key))

    @property
    def has_telegram(self) -> bool:
        return bool(
            _resolved("telegram_bot_token", self.telegram_bot_token)
            and _resolved("telegram_chat_id", self.telegram_chat_id)
        )

    @property
    def has_serper(self) -> bool:
        return bool(_resolved("serper_api_key", self.serper_api_key))

    @property
    def has_indexnow(self) -> bool:
        return bool(_resolved("indexnow_key", self.indexnow_key))

    @property
    def has_gemini(self) -> bool:
        return bool(_resolved("gemini_api_key", self.gemini_api_key))

    @property
    def has_anthropic(self) -> bool:
        return bool(_resolved("anthropic_api_key", self.anthropic_api_key))

    @property
    def has_auth_token(self) -> bool:
        # auth_token NO pasa por el resolver de la UI a propósito: se evalúa
        # al importar este módulo (antes de que exista conexión a la DB), y es
        # el gate de seguridad de S8 — debe seguir siendo solo de .env.
        return bool(self.auth_token)


def _resolved(field_name: str, env_value: str) -> str:
    """Valor efectivo de un secreto: gana lo guardado desde la UI de
    Configuración, con el de .env como respaldo. Import perezoso adentro de la
    función (no al tope del módulo) para no crear un ciclo — settings_store.py
    necesita `backend.db.database`, que a su vez importa `backend.config`."""
    try:
        from backend.settings_store import get_secret

        return get_secret(field_name, env_value)
    except Exception:  # noqa: BLE001 - S3: sin DB lista aún (primer arranque), usar .env
        return env_value


settings = Settings()

if settings.host != "127.0.0.1" and not settings.has_auth_token:
    raise RuntimeError(
        "HOST distinto de 127.0.0.1 requiere AUTH_TOKEN configurado en .env "
        "(regla S8 del PROMPT_MAESTRO) — exponer la API en la red sin "
        "autenticación es un riesgo real. Genera un token largo y aleatorio "
        "y agrégalo a .env antes de cambiar HOST."
    )


class _SecretRedactingFilter(logging.Filter):
    """Redacta cualquier secreto cargado para que nunca aparezca en logs (regla 4.1)."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


# Campos que pueden traer un secreto real — se redactan sea cual sea su
# origen (.env o guardado desde la UI de Configuración).
_REDACTED_SECRET_FIELDS = (
    "deepseek_api_key", "pagespeed_api_key", "bing_webmaster_api_key",
    "telegram_bot_token", "serper_api_key", "indexnow_key",
    "gemini_api_key", "anthropic_api_key",
)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    root = logging.getLogger()
    secrets = [_resolved(f, getattr(settings, f)) for f in _REDACTED_SECRET_FIELDS]
    secrets.append(settings.auth_token)  # este sí es siempre solo de .env
    secret_filter = _SecretRedactingFilter(secrets)
    for handler in root.handlers:
        handler.addFilter(secret_filter)


_SECRET_PATTERNS = re.compile(r"(sk-[a-zA-Z0-9]{10,}|AIza[a-zA-Z0-9_-]{20,})")


def looks_like_secret(text: str) -> bool:
    """Heurística usada por tests del Auditor de Seguridad para detectar fugas."""
    return bool(_SECRET_PATTERNS.search(text))
