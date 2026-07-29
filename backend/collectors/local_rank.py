"""Ranking real en el Local Pack de Google Maps vía Serper (§ mejoras 2026-07-25):
resuelve un hueco real — rank_tracking.py solo mide posición en resultados
ORGÁNICOS web, pero para negocios físicos (reparación de celulares, impresión)
la mayoría de búsquedas de intención de compra ("cerca de mí") activan el
Local Pack de Maps, no el listado orgánico. Mismo API key de Serper ya
configurado (SERPER_API_KEY), sin dependencia ni costo nuevo.

Verificado real contra la API el 2026-07-25 antes de escribir esto
(`POST https://google.serper.dev/places` con {"q","gl","hl","page"} contra
"reparacion de celulares cali"):
- Cada resultado trae `title`, `website`, `rating`, `ratingCount`, `position`.
- `position` es relativo A LA PÁGINA, igual que /search (confirmado probando
  page=2 en vivo: vuelve a traer 1-10) — se recalcula posición absoluta con
  el mismo cálculo `(page-1)*10 + position` que rank_tracking.py.
- No todos los negocios exponen `website` con su dominio propio (muchos solo
  ponen Instagram/WhatsApp ahí) — el match es SOLO por dominio propio, nunca
  por nombre parecido (evita falsos positivos con negocios de nombre similar
  o franquicias). Si el negocio no aparece con su dominio en las páginas
  consultadas, se guarda None — no se adivina cuál list de Maps "debe ser".

Honestidad de datos (regla P1): "no visto en el rango consultado" (None) es
un dato distinto de "no está en el Local Pack" — no pagamos por consultar
más allá de MAX_PAGES_PER_KEYWORD páginas.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.rank_tracking import _default_keywords
from backend.config import settings
from backend.settings_store import get_secret
from backend.db.database import get_connection, now_iso
from backend.db.schema import local_pack_rankings

logger = logging.getLogger(__name__)

SERPER_PLACES_URL = "https://google.serper.dev/places"
REQUEST_TIMEOUT = 15.0
RATE_LIMIT_DELAY_SECONDS = 0.3  # Serper permite 25 req/s, esto es solo cortesía

MAX_KEYWORDS_PER_RUN = 10
MAX_PAGES_PER_KEYWORD = 2  # hasta posición 20 del pack local, 1 crédito Serper por página


def _domain_of(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


class LocalRankCollector(BaseCollector):
    name = "local_rank"

    def __init__(self, project_slug: str, keywords: list[str] | None = None, max_keywords: int = MAX_KEYWORDS_PER_RUN):
        super().__init__(project_slug)
        self._requested_keywords = keywords
        self._max_keywords = max_keywords

    def collect(self) -> CollectorResult:
        if not settings.has_serper:
            return CollectorResult(
                status="skipped", raw_data=None,
                error_message="Ranking Local Pack sin configurar: falta SERPER_API_KEY en .env.",
            )

        keywords = self._requested_keywords or _default_keywords(self.project["id"], self._max_keywords)
        keywords = keywords[: self._max_keywords]
        if not keywords:
            return CollectorResult(
                status="skipped", raw_data=None,
                error_message="Sin keywords para verificar — ejecuta el collector de GSC primero.",
            )

        own_domain = _domain_of(self.project["url"])
        gl = (self.project.get("country") or "co").lower()
        hl = (self.project.get("language") or "es").lower()

        results = []
        errors = 0
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            for i, keyword in enumerate(keywords):
                if i > 0:
                    time.sleep(RATE_LIMIT_DELAY_SECONDS)
                try:
                    found = self._check_keyword(client, keyword, gl, hl, own_domain)
                    results.append({"keyword": keyword, **found})
                except httpx.HTTPError as exc:
                    logger.warning("Serper /places falló para '%s': %s", keyword, exc)
                    errors += 1

        if not results:
            return CollectorResult(
                status="error", raw_data=None,
                error_message=f"Ninguna keyword pudo consultarse ({errors} errores). Verifica SERPER_API_KEY.",
            )

        return CollectorResult(
            status="partial" if errors else "ok",
            raw_data={"own_domain": own_domain, "gl": gl, "hl": hl, "results": results, "errors": errors},
        )

    def _check_keyword(self, client: httpx.Client, keyword: str, gl: str, hl: str, own_domain: str) -> dict:
        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            response = client.post(
                SERPER_PLACES_URL,
                headers={"X-API-KEY": get_secret("serper_api_key", settings.serper_api_key), "Content-Type": "application/json"},
                json={"q": keyword, "gl": gl, "hl": hl, "page": page},
            )
            response.raise_for_status()
            places = response.json().get("places", [])
            for entry in places:
                if _domain_of(entry.get("website", "")) == own_domain:
                    absolute_position = (page - 1) * 10 + entry.get("position", 0)
                    return {
                        "our_position": absolute_position,
                        "our_listing_title": entry.get("title"),
                        "our_rating": entry.get("rating"),
                        "our_reviews_count": entry.get("ratingCount"),
                    }
            if len(places) < 10:
                break  # Google no tiene más resultados, no seguir paginando

        return {"our_position": None, "our_listing_title": None, "our_rating": None, "our_reviews_count": None}

    def persist_analysis(self, raw_data: dict) -> dict:
        today = now_iso()[:10]
        now = now_iso()
        with get_connection() as conn:
            for item in raw_data.get("results", []):
                values = {
                    "our_position": item["our_position"],
                    "our_listing_title": item["our_listing_title"],
                    "our_rating": item["our_rating"],
                    "our_reviews_count": item["our_reviews_count"],
                    "checked_at": now,
                }
                stmt = sqlite_insert(local_pack_rankings).values(
                    project_id=self.project["id"], keyword=item["keyword"], date=today, **values,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "date"], set_=values
                )
                conn.execute(stmt)

        checked = raw_data.get("results", [])
        in_pack = sum(1 for r in checked if r["our_position"] is not None)
        return {"keywords_checked": len(checked), "errors": raw_data.get("errors", 0), "our_domain_found_in": in_pack}


def run_local_rank_collector(project_slug: str, keywords: list[str] | None = None) -> dict:
    collector = LocalRankCollector(project_slug, keywords=keywords)
    snapshot_id = collector.run()

    with get_connection() as conn:
        from backend.db.schema import snapshots as snapshots_table

        row = conn.execute(
            select(
                snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message
            ).where(snapshots_table.c.id == snapshot_id)
        ).first()

    if row is None or row[1] in ("error", "skipped"):
        return {
            "snapshot_id": snapshot_id,
            "status": row[1] if row else "error",
            "summary": None,
            "message": row[2] if row else None,
        }

    summary = collector.persist_analysis(row[0])
    return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Collector de ranking en Local Pack de Maps vía Serper (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_local_rank_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
