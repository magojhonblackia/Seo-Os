"""Collector de Google Analytics 4 (§ herramientas nuevas 2026-07-23).

Por qué hace falta: hasta ahora solo teníamos CLICS de Search Console — o sea,
cuánta gente entra desde Google. GA4 responde lo que de verdad importa después
del clic: **qué páginas convierten**. Sin esto, el Action Plan prioriza por
impresiones/posición, no por dinero.

Sin dependencia nueva (regla P8): la Data API v1beta se consume con el
`google-api-python-client` que ya usamos para Search Console
(`build("analyticsdata", "v1beta")`, verificado en vivo 2026-07-23), y con el
MISMO service account — basta darle acceso de lectura a la propiedad GA4.

Se filtra a tráfico de Búsqueda Orgánica para que sea comparable con GSC.

Degradación con gracia (S3/P1): sin property_id o sin credenciales devuelve
'skipped' con instrucciones; si GA4 responde error de permisos, se reporta el
error real, nunca se inventan sesiones ni conversiones.
"""
from __future__ import annotations

import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import settings
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
DEFAULT_DAYS = 28
MAX_ROWS = 200

# GA4 renombró "conversions" a "keyEvents" (2024). Se intenta el nombre nuevo y
# se cae al viejo si la propiedad todavía responde con el esquema anterior —
# verificado como estrategia por el error real que devuelve la API ante una
# métrica desconocida, en vez de adivinar cuál soporta cada cuenta.
_METRIC_CANDIDATES = ["keyEvents", "conversions"]


def _build_service():
    creds = service_account.Credentials.from_service_account_file(
        str(settings.gsc_credentials_path), scopes=_SCOPES
    )
    return build("analyticsdata", "v1beta", credentials=creds, cache_discovery=False)


def resolve_property_id(project: dict) -> str:
    """Por proyecto primero (projects.config), luego el global del .env."""
    config = project.get("config") or {}
    return str(config.get("ga4_property_id") or get_secret("ga4_property_id", settings.ga4_property_id) or "").strip()


def _run_report(service, property_id: str, conversion_metric: str, days: int) -> dict:
    return (
        service.properties()
        .runReport(
            property=f"properties/{property_id}",
            body={
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "dimensions": [{"name": "landingPagePlusQueryString"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "totalUsers"},
                    {"name": conversion_metric},
                ],
                # Solo Búsqueda Orgánica: comparable con los clics de GSC.
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "sessionDefaultChannelGroup",
                        "stringFilter": {"value": "Organic Search"},
                    }
                },
                "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
                "limit": MAX_ROWS,
            },
        )
        .execute()
    )


def _parse_rows(response: dict, conversion_metric: str) -> list[dict]:
    rows = []
    for row in response.get("rows", []):
        dims = row.get("dimensionValues", [])
        mets = row.get("metricValues", [])
        if not dims or len(mets) < 3:
            continue

        def _num(idx: int) -> float:
            try:
                return float(mets[idx].get("value") or 0)
            except (TypeError, ValueError):
                return 0.0

        sessions = int(_num(0))
        conversions = _num(2)
        rows.append(
            {
                "landing_page": dims[0].get("value"),
                "sessions": sessions,
                "users": int(_num(1)),
                "conversions": conversions,
                "conversion_rate": round(conversions / sessions, 4) if sessions else None,
                "conversion_metric": conversion_metric,
            }
        )
    return rows


class GA4Collector(BaseCollector):
    name = "ga4"

    def __init__(self, project_slug: str, days: int = DEFAULT_DAYS):
        super().__init__(project_slug)
        self.days = days

    def collect(self) -> CollectorResult:
        property_id = resolve_property_id(self.project)
        if not property_id:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message=(
                    "GA4 sin configurar: agrega GA4_PROPERTY_ID en .env (el ID numérico de la "
                    "propiedad, no el 'G-XXXX') y dale acceso de lectura en GA4 al service "
                    "account de Search Console."
                ),
            )
        if not settings.has_gsc_credentials:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message="Falta credentials/gsc-service-account.json — GA4 usa el mismo service account.",
            )

        try:
            service = _build_service()
        except Exception as exc:  # noqa: BLE001
            return CollectorResult(status="error", raw_data=None, error_message=f"Credenciales inválidas: {exc}")

        last_error: str | None = None
        for metric in _METRIC_CANDIDATES:
            try:
                response = _run_report(service, property_id, metric, self.days)
            except HttpError as exc:
                last_error = str(exc)
                # métrica desconocida -> probar el nombre alternativo; otro error -> abortar
                if "keyEvents" in last_error or "conversions" in last_error or exc.resp.status == 400:
                    logger.info("GA4: métrica '%s' no aceptada, probando alternativa", metric)
                    continue
                return CollectorResult(
                    status="error",
                    raw_data=None,
                    error_message=(
                        f"GA4 rechazó la consulta ({exc.resp.status}). Verifica que el service account "
                        f"tenga acceso a la propiedad {property_id}. Detalle: {exc}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return CollectorResult(status="error", raw_data=None, error_message=f"Error consultando GA4: {exc}")

            rows = _parse_rows(response, metric)
            return CollectorResult(
                status="ok",
                raw_data={
                    "property_id": property_id,
                    "days": self.days,
                    "conversion_metric": metric,
                    "rows": rows,
                },
            )

        return CollectorResult(
            status="error",
            raw_data=None,
            error_message=f"GA4 no aceptó ninguna métrica de conversión conocida. Último error: {last_error}",
        )


def run_ga4_collector(project_slug: str, days: int = DEFAULT_DAYS) -> dict:
    collector = GA4Collector(project_slug, days=days)
    snapshot_id = collector.run()

    from sqlalchemy import select

    from backend.db.database import get_connection
    from backend.db.schema import snapshots as snapshots_table

    with get_connection() as conn:
        row = conn.execute(
            select(
                snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message
            ).where(snapshots_table.c.id == snapshot_id)
        ).first()

    raw = (row[0] or {}) if row and row[0] else {}
    rows = raw.get("rows", [])
    return {
        "snapshot_id": snapshot_id,
        "status": row[1] if row else "error",
        "summary": {
            "landing_pages": len(rows),
            "total_sessions": sum(r["sessions"] for r in rows),
            "total_conversions": round(sum(r["conversions"] for r in rows), 2),
            "conversion_metric": raw.get("conversion_metric"),
            "message": row[2] if row else None,
        },
    }
