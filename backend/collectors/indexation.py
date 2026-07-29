"""Collector de indexación real vía Search Console URL Inspection API.

A diferencia del semáforo técnico (que solo puede *inferir* indexabilidad a
partir de lo que el crawler propio observa: robots meta, canonical, status
code), esto pregunta a Google directamente qué hizo con cada URL — mismo
service account que ya usa `backend/collectors/gsc.py` (reutiliza
`_build_service()`, mismo scope `webmasters.readonly`, la API de
inspección de URLs vive en el mismo cliente `searchconsole` v1).

Verificado real el 2026-07-15 contra jcreparaciones.com:
- `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect`
  (vía `service.urlInspection().index().inspect(body=...)`), NO
  `runPagespeedInsights`-style guessing — se probó en vivo antes de escribir
  esto.
- Página indexada real: `verdict="PASS"`,
  `coverageState="Submitted and indexed"`.
- Página inexistente real: `verdict="NEUTRAL"`,
  `coverageState="URL is unknown to Google"` — Google no inventa que algo
  está indexado si no lo está, y nosotros tampoco (regla P1): se guarda el
  verdict/coverage_state tal cual vienen, sin reinterpretar.

Cuota real (verificada en docs oficiales, no supuesta): 600 QPM / 2000 QPD
por sitio — de sobra para sitios de este proyecto (15-50 páginas). Rate
limit propio de 1 req/s (mismo criterio ético que crawler.py) para no
acercarse al límite por minuto en sitios grandes.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.collectors import progress
from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.gsc import _build_service
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import indexation_status, pages

logger = logging.getLogger(__name__)

MAX_URLS_PER_RUN = 50
RATE_LIMIT_DELAY_SECONDS = 1.0


class IndexationCollector(BaseCollector):
    name = "indexation"

    def collect(self) -> CollectorResult:
        if not settings.has_gsc_credentials:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message=(
                    "Indexación sin configurar: falta credentials/gsc-service-account.json "
                    "(la misma credencial que usa el collector de GSC)."
                ),
            )

        with get_connection() as conn:
            page_rows = conn.execute(
                select(pages.c.url).where(pages.c.project_id == self.project["id"]).limit(MAX_URLS_PER_RUN)
            ).all()

        if not page_rows:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message="Sin páginas crawleadas aún — ejecuta el crawler técnico primero.",
            )

        site_url = self.project["gsc_property"]
        try:
            service = _build_service()
        except (OSError, ValueError) as exc:
            logger.warning("Credenciales GSC inválidas o ilegibles: %s", exc)
            return CollectorResult(status="error", raw_data=None, error_message=f"Credenciales GSC inválidas: {exc}")

        slug = self.project["slug"]
        # § 2026-07-24: hasta 50 URLs contra la URL Inspection API de Google,
        # que es lenta de verdad (~6-7s por llamada) — sin esto, 50 URLs
        # tardan ~6 minutos con la UI mostrando "Paso N/M" sin ningún cambio,
        # indistinguible de estar congelada (bug real reportado por el usuario).
        progress.start(slug, len(page_rows), phase="checking_indexation")
        results = []
        errors = 0
        try:
            for i, row in enumerate(page_rows):
                if i > 0:
                    time.sleep(RATE_LIMIT_DELAY_SECONDS)
                try:
                    response = service.urlInspection().index().inspect(
                        body={"inspectionUrl": row.url, "siteUrl": site_url}
                    ).execute()
                    results.append({"url": row.url, "result": response.get("inspectionResult", {})})
                except (HttpError, OSError) as exc:
                    # OSError cubre el timeout de red (bug real 2026-07-26: sin
                    # timeout en el transporte HTTP, una URL que no responde
                    # colgaba el hilo entero para siempre — ver _build_service()
                    # en gsc.py). Una URL que falla no debe tumbar las otras 49.
                    logger.warning("Inspección de %s falló: %s", row.url, exc)
                    errors += 1
                progress.update(slug, i + 1, row.url)
        finally:
            progress.clear(slug)

        if not results:
            return CollectorResult(
                status="error",
                raw_data=None,
                error_message=(
                    f"Ninguna URL pudo inspeccionarse ({errors} errores). "
                    "¿La cuenta de servicio tiene acceso a esta propiedad en Search Console?"
                ),
            )

        return CollectorResult(
            status="partial" if errors else "ok",
            raw_data={"site_url": site_url, "results": results, "errors": errors},
        )

    def persist_analysis(self, raw_data: dict) -> dict:
        now = now_iso()
        counts: dict[str, int] = {}

        with get_connection() as conn:
            for item in raw_data.get("results", []):
                url = item["url"]
                idx = item["result"].get("indexStatusResult", {})
                verdict = idx.get("verdict", "VERDICT_UNSPECIFIED")
                counts[verdict] = counts.get(verdict, 0) + 1

                values = {
                    "verdict": verdict,
                    "coverage_state": idx.get("coverageState"),
                    "robots_txt_state": idx.get("robotsTxtState"),
                    "indexing_state": idx.get("indexingState"),
                    "page_fetch_state": idx.get("pageFetchState"),
                    "google_canonical": idx.get("googleCanonical"),
                    "user_canonical": idx.get("userCanonical"),
                    "crawled_as": idx.get("crawledAs"),
                    "last_google_crawl": idx.get("lastCrawlTime"),
                    "checked_at": now,
                }
                stmt = sqlite_insert(indexation_status).values(
                    project_id=self.project["id"], url=url, **values,
                )
                stmt = stmt.on_conflict_do_update(index_elements=["project_id", "url"], set_=values)
                conn.execute(stmt)

        return {"urls_checked": len(raw_data.get("results", [])), "errors": raw_data.get("errors", 0), "by_verdict": counts}


def run_indexation_collector(project_slug: str) -> dict:
    collector = IndexationCollector(project_slug)
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
    parser = argparse.ArgumentParser(description="Collector de indexación real (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_indexation_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
