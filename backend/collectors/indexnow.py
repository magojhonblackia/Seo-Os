"""IndexNow: protocolo abierto (Bing, Yandex, Seznam, Naver) para notificar que
una URL cambió, sin esperar a que el buscador vuelva a crawlear por su cuenta
(§ herramientas de mercado 2026-07-24, adaptado de indexnow_checker.py).

Por qué hace falta: hasta ahora solo hablamos con Google (GSC/Indexing). GSC no
tiene un canal así de directo salvo la URL Inspection API (con cuota estricta,
50/corrida). IndexNow es gratis, sin cuota real para un sitio de este tamaño, y
un solo endpoint HTTP.

Diseño deliberado — CHECK y SUBMIT están separados:
- El check (¿está el key file publicado?) SÍ corre en la auditoría normal:
  es una lectura, sin efecto en terceros.
  Verificado en jcreparaciones.com (2026-07-24): no tiene IndexNow configurado
  — es una función a OFRECER, no un problema roto que arreglar.
- El submit (avisar a Bing/Yandex que estas URLs cambiaron) es una acción
  EXPLÍCITA del usuario, nunca automática dentro de la secuencia de auditoría
  — notifica a un tercero real cada vez que se llama, así que no debe
  dispararse sin que el usuario lo pida (mismo criterio que rank_tracking:
  manual, no en cada corrida).
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import urljoin, urlparse

import httpx

from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import REQUEST_TIMEOUT, USER_AGENT
from backend.config import settings
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

INDEXNOW_ENDPOINTS = {
    "bing": "https://www.bing.com/indexnow",
    "yandex": "https://yandex.com/indexnow",
    "seznam": "https://search.seznam.cz/indexnow",
    "naver": "https://searchadvisor.naver.com/indexnow",
}
MAX_URLS_PER_SUBMIT = 10_000  # límite del protocolo


def generate_key() -> str:
    """Genera una key válida para IndexNow: 32-128 caracteres hex."""
    return secrets.token_hex(16)


def _key_file_url(site_url: str, key: str) -> str:
    return urljoin(site_url, f"/{key}.txt")


class IndexNowCollector(BaseCollector):
    name = "indexnow"

    def collect(self) -> CollectorResult:
        if not settings.has_indexnow:
            suggested = generate_key()
            return CollectorResult(
                status="skipped",
                raw_data={"configured": False, "suggested_key": suggested},
                error_message=(
                    "IndexNow sin configurar. Pasos: 1) agrega INDEXNOW_KEY="
                    f"{suggested} a tu .env (o genera tu propia key), "
                    f"2) publica un archivo /{suggested}.txt en la raíz de tu sitio "
                    "con esa key como único contenido, 3) vuelve a correr esta auditoría."
                ),
            )

        key = get_secret("indexnow_key", settings.indexnow_key)
        site_url = self.project["url"]
        key_url = _key_file_url(site_url, key)
        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                response = client.get(key_url)
        except httpx.HTTPError as exc:
            return CollectorResult(
                status="error", raw_data=None, error_message=f"No se pudo verificar {key_url}: {exc}"
            )

        content = response.text.strip() if response.status_code == 200 else None
        matches = content == key
        return CollectorResult(
            status="ok",
            raw_data={
                "configured": True,
                "key": key,
                "key_url": key_url,
                "file_found": response.status_code == 200,
                "key_matches": matches,
            },
        )


def run_indexnow_check(project_slug: str) -> dict:
    collector = IndexNowCollector(project_slug)
    snapshot_id = collector.run()

    from sqlalchemy import select

    from backend.db.database import get_connection
    from backend.db.schema import snapshots as snapshots_table

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message).where(
                snapshots_table.c.id == snapshot_id
            )
        ).first()

    raw = (row[0] or {}) if row else {}
    return {
        "snapshot_id": snapshot_id,
        "status": row[1] if row else "error",
        "summary": {**raw, "message": row[2] if row else None},
    }


def submit_urls(site_url: str, urls: list[str], engine: str = "bing") -> dict:
    """Acción MANUAL explícita (nunca automática): notifica a un motor de
    búsqueda real que estas URLs cambiaron. Requiere IndexNow ya configurado
    (key file publicado y verificado por run_indexnow_check)."""
    if not settings.has_indexnow:
        raise ValueError("IndexNow no está configurado (falta INDEXNOW_KEY)")
    if engine not in INDEXNOW_ENDPOINTS:
        raise ValueError(f"Motor desconocido: {engine}. Usa uno de {sorted(INDEXNOW_ENDPOINTS)}")
    if not urls:
        raise ValueError("No hay URLs para enviar")
    if len(urls) > MAX_URLS_PER_SUBMIT:
        raise ValueError(f"IndexNow acepta máximo {MAX_URLS_PER_SUBMIT} URLs por envío")

    host = urlparse(site_url).netloc
    payload = {"host": host, "key": get_secret("indexnow_key", settings.indexnow_key), "urlList": urls}
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}, timeout=REQUEST_TIMEOUT) as client:
        response = client.post(INDEXNOW_ENDPOINTS[engine], json=payload)

    return {
        "engine": engine,
        "urls_submitted": len(urls),
        "status_code": response.status_code,
        "accepted": response.status_code in (200, 202),
    }
