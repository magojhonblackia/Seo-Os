"""Collector autónomo de Google Search Console (§9 Fase 4).

Hasta ahora los rankings solo se cargaban una vez, a mano, vía
`scripts/bootstrap_data.py` (la conexión GSC real solo existía dentro de la
sesión de Claude vía MCP). Este collector usa una service account (regla S8:
credencial de servidor-a-servidor, sin login interactivo — apta para el
scheduler diario sin supervisión) para traer datos reales y frescos por su
cuenta.

Degradación elegante (S3):
- Sin `credentials/gsc-service-account.json`: status="skipped", nunca falla.
- Si la propiedad de este proyecto no está autorizada para esa cuenta de
  servicio en Search Console (verificado 2026-07-12 contra credenciales
  reales: pasa con jcreparaciones.com y komaromiprintservice.com, falla con
  soyfixio.com y tech.soyfixio.com porque el email de la cuenta de servicio
  aún no se agregó ahí): status="error" con mensaje claro, nunca inventa
  datos (P1).

Ventana de fechas: Search Console tiene ~2-3 días de latencia en sus datos
más recientes, así que la consulta termina GSC_END_LAG_DAYS atrás respecto a
hoy. Ventana de GSC_LOOKBACK_DAYS días (muy por debajo del límite duro de 16
meses de §4.3 del PROMPT_MAESTRO) — correr esto a diario mantiene los datos
al día sin traer de más cada vez (upsert idempotente, regla S5).
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta

import httplib2
from google.oauth2 import service_account
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_daily, gsc_queries

logger = logging.getLogger(__name__)

GSC_LOOKBACK_DAYS = 30
GSC_END_LAG_DAYS = 3
MAX_QUERY_ROWS = 1000  # límite duro (regla §4.3: page_size ≤ 1000)
MAX_LOOKBACK_DAYS = 480  # ~16 meses, el límite real de datos de Search Console
_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


_HTTP_TIMEOUT_SECONDS = 30  # bug real 2026-07-26: sin esto, .execute() puede
# colgarse indefinidamente si Google no responde (ni error ni timeout) — el
# usuario reportó la auditoría "congelada" en indexación en 0/50 URLs, sin
# avanzar nunca. httplib2.Http() no tiene timeout por defecto.


def _build_service():
    creds = service_account.Credentials.from_service_account_file(
        str(settings.gsc_credentials_path), scopes=_SCOPES
    )
    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS))
    return build("searchconsole", "v1", http=authorized_http, cache_discovery=False)


class GscCollector(BaseCollector):
    name = "gsc"

    def __init__(self, project_slug: str, lookback_days: int = GSC_LOOKBACK_DAYS):
        super().__init__(project_slug)
        # § 2026-07-27: Search Console deja elegir período (7d/28d/3-16 meses)
        # en su propia UI — antes esto era un valor fijo de 30 días. Se acota
        # al límite duro ya documentado (16 meses, §4.3 del PROMPT_MAESTRO)
        # para no pedir sin querer una ventana que la API rechaza.
        self.lookback_days = max(1, min(lookback_days, MAX_LOOKBACK_DAYS))

    def collect(self) -> CollectorResult:
        if not settings.has_gsc_credentials:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message=(
                    "GSC sin configurar: falta credentials/gsc-service-account.json "
                    "(ver README, sección 'Traer más datos reales de Search Console')."
                ),
            )

        site_url = self.project["gsc_property"]
        end_date = date.today() - timedelta(days=GSC_END_LAG_DAYS)
        start_date = end_date - timedelta(days=self.lookback_days)

        try:
            service = _build_service()
            daily_response = service.searchanalytics().query(
                siteUrl=site_url,
                body={
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "dimensions": ["date"],
                    "rowLimit": GSC_LOOKBACK_DAYS + 1,
                },
            ).execute()
            query_response = service.searchanalytics().query(
                siteUrl=site_url,
                body={
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "dimensions": ["query", "page"],
                    "rowLimit": MAX_QUERY_ROWS,
                },
            ).execute()
        except HttpError as exc:
            logger.warning("GSC API falló para %s (%s): %s", self.project["slug"], site_url, exc)
            return CollectorResult(
                status="error",
                raw_data=None,
                error_message=(
                    f"No se pudo consultar '{site_url}' en Search Console (HTTP {exc.status_code}). "
                    "¿La cuenta de servicio tiene acceso a esta propiedad? Agrégala en "
                    "Search Console → Configuración → Usuarios y permisos."
                ),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Credenciales GSC inválidas o ilegibles: %s", exc)
            return CollectorResult(status="error", raw_data=None, error_message=f"Credenciales GSC inválidas: {exc}")

        return CollectorResult(
            status="ok",
            raw_data={
                "site_url": site_url,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "daily": daily_response.get("rows", []),
                "queries": query_response.get("rows", []),
            },
        )

    def persist_analysis(self, raw_data: dict) -> dict:
        now = now_iso()
        end_date = raw_data["end_date"]

        with get_connection() as conn:
            daily_rows = 0
            for row in raw_data.get("daily", []):
                row_date = row["keys"][0]
                impressions = row.get("impressions", 0)
                clicks = row.get("clicks", 0)
                ctr = row.get("ctr", 0.0)
                position = row.get("position", 0.0)
                stmt = sqlite_insert(gsc_daily).values(
                    project_id=self.project["id"], date=row_date, clicks=clicks,
                    impressions=impressions, ctr=ctr, position=position, created_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "date"],
                    set_={"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position},
                )
                conn.execute(stmt)
                daily_rows += 1

            query_rows = 0
            for row in raw_data.get("queries", []):
                query, page = row["keys"][0], row["keys"][1]
                impressions = row.get("impressions", 0)
                clicks = row.get("clicks", 0)
                ctr = row.get("ctr", 0.0)
                position = row.get("position", 0.0)
                stmt = sqlite_insert(gsc_queries).values(
                    project_id=self.project["id"], date=end_date, query=query, page=page,
                    clicks=clicks, impressions=impressions, ctr=ctr, position=position, created_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "date", "query", "page"],
                    set_={"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position},
                )
                conn.execute(stmt)
                query_rows += 1

        return {
            "daily_rows": daily_rows,
            "query_rows": query_rows,
            "start_date": raw_data["start_date"],
            "end_date": end_date,
        }


def run_gsc_collector(project_slug: str, lookback_days: int = GSC_LOOKBACK_DAYS) -> dict:
    collector = GscCollector(project_slug, lookback_days=lookback_days)
    snapshot_id = collector.run()

    with get_connection() as conn:
        from sqlalchemy import select

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
    parser = argparse.ArgumentParser(description="Collector GSC (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_gsc_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
