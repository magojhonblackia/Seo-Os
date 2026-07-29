"""Collector de Core Web Vitals vía Google PageSpeed Insights API v5.

La clave estaba configurada desde hace semanas (`PAGESPEED_API_KEY` en
`.env`, verificada por curl) pero sin ningún collector construido encima —
era la deuda técnica más grande pendiente del proyecto. Endpoint real
verificado contra `jcreparaciones.com` y `wikipedia.org` el 2026-07-15:
`GET /pagespeedonline/v5/runPagespeed` (nota: NO "runPagespeedInsights",
que es el nombre intuitivo pero da 404 real).

Trae dos fuentes de datos, y hay que ser honesto sobre cuál es cuál (P1):
- **Lab data** (`lighthouseResult`): simulado, siempre disponible para
  cualquier URL pública. De aquí salen los 4 scores 0-100 (Performance,
  Accessibility, Best Practices, SEO) y los Core Web Vitals de laboratorio.
- **Field data** (`loadingExperience.metrics`, CrUX): mediciones reales de
  usuarios de Chrome. Solo existe si Google tiene tráfico suficiente del
  sitio — verificado real: `jcreparaciones.com` no tiene (el objeto
  `loadingExperience` existe pero `metrics` es `{}`), `wikipedia.org` sí.
  Para un sitio local/pequeño como los de este proyecto, lo normal es NO
  tener field data — se declara `field_data_available=False` en vez de
  rellenar con el dato de laboratorio disfrazado de dato de campo.

Solo estrategia "mobile" por ahora (mobile-first indexing de Google es la
que más importa) — "desktop" queda como ampliación futura, la tabla
`pagespeed` ya tiene la columna `strategy` lista para eso.

§ herramientas de mercado 2026-07-24 — CWV por página, no solo la home: con
~330 páginas programáticas (/reparacion/marca/modelo/servicio), medir solo la
home no dice nada de las demás. PageSpeed Insights no tiene un límite de cuota
documentado agresivo para uso normal, pero cada consulta tarda 15-30s (corre un
Lighthouse real en el servidor de Google) — así que se ACOTA a un número
manejable de páginas por corrida (home + las de más impresiones reales en GSC,
que es donde más importa la velocidad) en vez de todo el sitio de una vez.
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import settings
from backend.settings_store import get_secret
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, pagespeed

logger = logging.getLogger(__name__)

PAGESPEED_API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
REQUEST_TIMEOUT = 60.0  # Lighthouse real corre en el servidor de Google, tarda 15-30s
STRATEGY = "mobile"
DEFAULT_MAX_PAGES = 6  # home + 5 top por impresiones reales — cada una tarda 15-30s
MIN_DELAY_BETWEEN_CALLS = 1.0  # cortesía hacia la API de Google, no tiene cuota dura documentada

_AUDIT_KEYS = {
    "lcp_ms": "largest-contentful-paint",
    "cls": "cumulative-layout-shift",
    "tbt_ms": "total-blocking-time",
    "fcp_ms": "first-contentful-paint",
    "si_ms": "speed-index",
}


# Umbrales oficiales de Google para CWV ("Needs improvement" / "Poor") — se
# usan para generar issues reales, cosa que este collector no hacía antes
# (solo guardaba los números, sin traducirlos a un hallazgo accionable).
_CWV_THRESHOLDS = {
    "lcp_ms": (2500, 4000),
    "cls": (0.1, 0.25),
    "tbt_ms": (200, 600),  # proxy de laboratorio para INP (que solo existe como field data)
}


def select_target_urls(project: dict, max_pages: int = DEFAULT_MAX_PAGES) -> list[str]:
    """Home siempre incluida + las páginas con más impresiones REALES en GSC
    (donde más importa la velocidad, no una muestra al azar del crawler).

    Dedup por URL CANÓNICA (no por string exacto): 'https://x.com' y
    'https://x.com/' son la misma página, y GSC casi siempre reporta la home
    con slash final — sin esto, un slot se desperdiciaba en un "duplicado"
    real de la home (verificado en vivo con datos de jcreparaciones.com)."""
    from backend.analyzers.coverage import canonical_url

    home = project["url"]
    urls = [home]
    seen_canonical = {canonical_url(home)}
    impressions_sum = func.sum(gsc_queries.c.impressions)
    with get_connection() as conn:
        latest_date = conn.execute(
            select(gsc_queries.c.date).where(gsc_queries.c.project_id == project["id"]).order_by(desc(gsc_queries.c.date)).limit(1)
        ).scalar()
        if latest_date:
            rows = conn.execute(
                select(gsc_queries.c.page, impressions_sum)
                .where(gsc_queries.c.project_id == project["id"], gsc_queries.c.date == latest_date, gsc_queries.c.page.is_not(None))
                .group_by(gsc_queries.c.page)
                .order_by(desc(impressions_sum))
                .limit(max_pages * 3)  # margen por duplicados canónicos (www, slash, home)
            ).all()
            for page, _impr in rows:
                if len(urls) >= max_pages:
                    break
                canon = canonical_url(page) if page else None
                if canon and canon not in seen_canonical:
                    seen_canonical.add(canon)
                    urls.append(page)
    return urls[:max_pages]


class PagespeedCollector(BaseCollector):
    name = "pagespeed"

    def __init__(self, project_slug: str, max_pages: int = DEFAULT_MAX_PAGES):
        super().__init__(project_slug)
        self.max_pages = max_pages

    def _fetch_one(self, client: httpx.Client, url: str) -> dict:
        params = {
            "url": url,
            "key": get_secret("pagespeed_api_key", settings.pagespeed_api_key),
            "strategy": STRATEGY,
            "category": ["performance", "accessibility", "best-practices", "seo"],
        }
        response = client.get(PAGESPEED_API_URL, params=params)
        response.raise_for_status()
        return response.json()

    def collect(self) -> CollectorResult:
        if not settings.has_pagespeed:
            return CollectorResult(
                status="skipped",
                raw_data=None,
                error_message="PageSpeed Insights sin configurar: falta PAGESPEED_API_KEY en .env.",
            )

        target_urls = select_target_urls(self.project, self.max_pages)
        pages_data: list[dict] = []
        errors: list[str] = []

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            for i, url in enumerate(target_urls):
                try:
                    data = self._fetch_one(client, url)
                    pages_data.append({"url": url, "data": data})
                except httpx.HTTPStatusError as exc:
                    logger.warning("PageSpeed API falló para %s: HTTP %s", url, exc.response.status_code)
                    errors.append(f"{url}: HTTP {exc.response.status_code}")
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("PageSpeed API falló para %s: %s", url, exc)
                    errors.append(f"{url}: {exc}")
                if i < len(target_urls) - 1:
                    time.sleep(MIN_DELAY_BETWEEN_CALLS)

        if not pages_data:
            return CollectorResult(
                status="error", raw_data=None,
                error_message="; ".join(errors[:3]) if errors else "PageSpeed Insights no devolvió datos para ninguna URL.",
            )
        return CollectorResult(
            status="partial" if errors else "ok",
            raw_data={"pages": pages_data, "errors": errors},
            error_message="; ".join(errors[:3]) if errors else None,
        )

    def _parse_one(self, url: str, raw_data: dict) -> dict:
        lighthouse = raw_data.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})
        audits = lighthouse.get("audits", {})

        def _cat_score(key: str) -> int | None:
            score = categories.get(key, {}).get("score")
            return round(score * 100) if score is not None else None

        def _audit_value(audit_key: str) -> float | None:
            value = audits.get(audit_key, {}).get("numericValue")
            return round(value) if value is not None else None

        field_metrics = raw_data.get("loadingExperience", {}).get("metrics", {})
        field_data_available = bool(field_metrics)

        def _field_percentile(metric: str) -> float | None:
            return field_metrics.get(metric, {}).get("percentile")

        cls_value = audits.get(_AUDIT_KEYS["cls"], {}).get("numericValue")
        field_cls_percentile = _field_percentile("CUMULATIVE_LAYOUT_SHIFT_SCORE")

        return {
            "url": url,
            "performance_score": _cat_score("performance"),
            "accessibility_score": _cat_score("accessibility"),
            "best_practices_score": _cat_score("best-practices"),
            "seo_score": _cat_score("seo"),
            "lcp_ms": _audit_value(_AUDIT_KEYS["lcp_ms"]),
            "cls": round(cls_value, 3) if cls_value is not None else None,
            "tbt_ms": _audit_value(_AUDIT_KEYS["tbt_ms"]),
            "fcp_ms": _audit_value(_AUDIT_KEYS["fcp_ms"]),
            "si_ms": _audit_value(_AUDIT_KEYS["si_ms"]),
            "field_data_available": field_data_available,
            "field_lcp_ms": _field_percentile("LARGEST_CONTENTFUL_PAINT_MS"),
            "field_cls": round(field_cls_percentile / 100, 3) if field_cls_percentile is not None else None,
            "field_inp_ms": _field_percentile("INTERACTION_TO_NEXT_PAINT"),
        }

    def persist_analysis(self, snapshot_id: int, raw_data: dict) -> dict:
        today = now_iso()[:10]
        parsed = [self._parse_one(p["url"], p["data"]) for p in raw_data.get("pages", [])]

        with get_connection() as conn:
            for values in parsed:
                row_values = {k: v for k, v in values.items() if k != "url"}
                stmt = sqlite_insert(pagespeed).values(
                    project_id=self.project["id"], date=today, strategy=STRATEGY, url=values["url"],
                    created_at=now_iso(), **row_values,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "date", "strategy", "url"], set_=row_values
                )
                conn.execute(stmt)

            found_issues = build_cwv_issues(parsed)
            created = 0
            by_severity = {"critical": 0, "high": 0, "medium": 0}
            for issue in found_issues:
                if record_issue(conn, project_id=self.project["id"], snapshot_id=snapshot_id, page_id=None, issue=issue, now=now_iso()):
                    created += 1
                    by_severity[issue.severity] += 1

            resolved = reconcile_project_issues(
                conn,
                project_id=self.project["id"],
                owned_categories=CWV_OWNED_CATEGORIES,
                fresh_keys={(i.category, i.title) for i in found_issues},
                now=now_iso(),
            )

        return {
            "date": today, "strategy": STRATEGY, "pages_checked": len(parsed),
            "issues_created": created, "issues_resolved": resolved, "by_severity": by_severity,
            "pages": parsed,
        }


CWV_OWNED_CATEGORIES = {"performance_cwv"}


def build_cwv_issues(pages: list[dict]) -> list[dict]:
    """Agrupa por métrica (no un issue por página — con 6 páginas lentas sería
    ruido) usando los umbrales oficiales 'Needs improvement'/'Poor' de Google."""
    from backend.analyzers.mago import MagoIssue

    issues: list[MagoIssue] = []
    labels = {"lcp_ms": "LCP", "cls": "CLS", "tbt_ms": "TBT (proxy de INP)"}
    for metric, (needs_improvement, poor) in _CWV_THRESHOLDS.items():
        poor_pages = [p for p in pages if p.get(metric) is not None and p[metric] >= poor]
        warn_pages = [
            p for p in pages if p.get(metric) is not None and needs_improvement <= p[metric] < poor
        ]
        if poor_pages:
            issues.append(
                MagoIssue(
                    severity="high",
                    category="performance_cwv",
                    title=f"{labels[metric]} en rango POOR en {len(poor_pages)} página(s) (lab data, PageSpeed Insights)",
                    current="; ".join(f"{p['url']} ({p[metric]})" for p in poor_pages[:5]),
                    suggested=f"{labels[metric]} por encima de {poor} — revisa estas páginas con el reporte completo de PageSpeed Insights para la causa específica.",
                    effort="1d",
                    impact=4,
                )
            )
        if warn_pages:
            issues.append(
                MagoIssue(
                    severity="medium",
                    category="performance_cwv",
                    title=f"{labels[metric]} en rango 'necesita mejora' en {len(warn_pages)} página(s)",
                    current="; ".join(f"{p['url']} ({p[metric]})" for p in warn_pages[:5]),
                    suggested=f"{labels[metric]} entre {needs_improvement} y {poor} — mejorable pero no crítico.",
                    effort="1d",
                    impact=2,
                )
            )
    return issues


def run_pagespeed_collector(project_slug: str, max_pages: int = DEFAULT_MAX_PAGES) -> dict:
    collector = PagespeedCollector(project_slug, max_pages=max_pages)
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

    summary = collector.persist_analysis(snapshot_id, row[0])
    return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Collector PageSpeed Insights (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_pagespeed_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
