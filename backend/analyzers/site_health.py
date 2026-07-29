"""Orquesta los analyzers nuevos (cobertura, enlazado interno, duplicados/thin)
sobre datos YA recolectados (§ herramientas nuevas 2026-07-23) — sin red.

Fuentes (todas existentes, regla S2: el crudo ya está guardado):
- snapshot más reciente del collector 'crawler' → páginas con internal_links,
  status_code y redirected_to,
- snapshot más reciente del collector 'sitemap' → URLs declaradas,
- tabla indexation_status → qué tiene Google realmente indexado (verdict PASS),
- tabla pages → title/meta/H1/word_count para duplicados y thin.

P1: cada fuente ausente se reporta como ausente (None), nunca se rellena.
"""
from __future__ import annotations

from sqlalchemy import desc, insert, select

from backend.analyzers.coverage import (
    build_coverage_issues,
    build_redirect_map,
    build_robots_sitemap_conflict_issues,
    coverage_diff,
    find_broken_pages,
    find_orphans,
    find_redirect_links,
    find_robots_sitemap_conflicts,
)
from backend.analyzers.duplicates import (
    build_duplicate_issues,
    find_duplicate_field,
    find_thin_content,
)
from backend.analyzers.cache_headers import analyze_cache_headers, build_cache_headers_issues
from backend.analyzers.internal_links import analyze_internal_links, build_internal_link_issues
from backend.analyzers.schema_validation import build_schema_issues, validate_pages_schema
from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.db.database import get_connection, now_iso
from backend.db.schema import indexation_status, pages, projects, snapshots

# Categorías que ESTE análisis recalcula por completo en cada corrida — se
# reconcilian al terminar para que un problema ya resuelto no quede 'open'.
SITE_HEALTH_CATEGORIES = {"coverage", "internal_links", "duplicates", "schema", "performance"}


def _latest_snapshot_raw(conn, project_id: int, collector: str) -> dict | None:
    row = conn.execute(
        select(snapshots.c.raw_data)
        .where(
            snapshots.c.project_id == project_id,
            snapshots.c.collector == collector,
            snapshots.c.status.in_(["ok", "partial"]),
        )
        .order_by(desc(snapshots.c.id))
        .limit(1)
    ).first()
    return (row[0] or {}) if row else None


def run_site_health_analysis(project_id: int) -> dict:
    now = now_iso()
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.id == project_id)).first()
        if project is None:
            raise ValueError(f"Proyecto {project_id} no existe")
        home_url = project.url

        crawler_raw = _latest_snapshot_raw(conn, project_id, "crawler")
        if crawler_raw is None:
            snapshot_id = conn.execute(
                insert(snapshots).values(
                    project_id=project_id, collector="site_health", status="skipped",
                    started_at=now, finished_at=now_iso(),
                    error_message="Sin datos del crawler aún — ejecuta una auditoría primero",
                    raw_data=None, created_at=now_iso(),
                )
            ).inserted_primary_key[0]
            return {"snapshot_id": snapshot_id, "status": "skipped", "issues_created": 0}

        crawled_pages = crawler_raw.get("pages", [])
        sitemap_raw = _latest_snapshot_raw(conn, project_id, "sitemap")
        sitemap_urls = sitemap_raw.get("urls") if sitemap_raw else None

        # TODAS las URLs que realmente consultamos a la URL Inspection API
        # (tenga el verdict que tenga). Sin esto no se puede afirmar "no
        # indexada" sin mentir: la API tiene cuota y solo vemos un subconjunto.
        inspected_rows = conn.execute(
            select(indexation_status.c.url, indexation_status.c.verdict).where(
                indexation_status.c.project_id == project_id
            )
        ).all()
        # None (no lista vacía) si nunca corrimos indexación: "no lo sabemos" ≠ "cero indexadas"
        inspected_urls = [r.url for r in inspected_rows] if inspected_rows else None
        indexed_urls = [r.url for r in inspected_rows if r.verdict == "PASS"] if inspected_rows else None

        page_rows = [dict(r._mapping) for r in conn.execute(select(pages).where(pages.c.project_id == project_id)).all()]

        # ---- analyzers puros ----
        # El mapa de redirects es lo que evita la mayor familia de falsos
        # positivos: un alias 301/308 no es una página duplicada ni thin.
        # Se combina la tabla `pages` PERSISTIDA (acumula cobertura de todos los
        # crawls anteriores) con el crawl fresco (más reciente, gana en caso de
        # conflicto) — bug real 2026-07-24: usar SOLO el crawl más reciente
        # perdía las URLs viejas que ya nadie enlaza y no se re-visitan, así
        # que su redirect se "olvidaba" y volvían a aparecer como duplicado.
        redirect_map = {**build_redirect_map(page_rows), **build_redirect_map(crawled_pages)}
        orphans = find_orphans(crawled_pages, home_url)
        broken = find_broken_pages(crawled_pages)
        redirects = find_redirect_links(crawled_pages)
        diff = coverage_diff(sitemap_urls, crawled_pages, indexed_urls, inspected_urls)
        links = analyze_internal_links(crawled_pages, home_url)
        dup_titles = find_duplicate_field(page_rows, "title", redirect_map)
        dup_metas = find_duplicate_field(page_rows, "meta_description", redirect_map)
        dup_h1s = find_duplicate_field(page_rows, "h1", redirect_map)

        # Último recurso, acotado a lo que YA salió como duplicado: una URL
        # huérfana (nadie la enlaza) nunca se re-crawlea, así que su
        # redirected_to persistido puede quedar desactualizado para siempre —
        # bug real 2026-07-24 (/reparacion-iphone-buenaventura etc. seguían
        # marcándose duplicadas). Se resuelve por red SOLO las URLs de los
        # grupos ya detectados (típicamente un puñado, no todo el sitio) —
        # mismo patrón ya usado en opportunities.py para canibalización.
        dup_candidate_urls = sorted(
            {u for group in (*dup_titles, *dup_metas, *dup_h1s) for u in group["urls"]}
        )
        if dup_candidate_urls:
            try:
                from backend.collectors.redirects import resolve_redirect_targets

                newly_resolved = resolve_redirect_targets(dup_candidate_urls, redirect_map)
            except Exception as exc:  # noqa: BLE001 - S3: sin resolución, seguimos con lo que había
                import logging

                logging.getLogger(__name__).warning("Resolución de redirects para duplicados falló: %s", exc)
                newly_resolved = {}
            if newly_resolved:
                redirect_map = {**redirect_map, **newly_resolved}
                dup_titles = find_duplicate_field(page_rows, "title", redirect_map)
                dup_metas = find_duplicate_field(page_rows, "meta_description", redirect_map)
                dup_h1s = find_duplicate_field(page_rows, "h1", redirect_map)

        thin = find_thin_content(page_rows, redirect_map=redirect_map)
        schema_validation = validate_pages_schema(crawled_pages)
        cache_analysis = analyze_cache_headers(crawled_pages)

        # § mejoras 2026-07-25: 1 sola petición a robots.txt (no por página) para
        # detectar URLs que el sitemap declara pero robots.txt bloquea — mensaje
        # contradictorio real ("indexa esto" + "no lo rastrees"). Degradación con
        # gracia (S3): si robots.txt no se puede leer, simplemente no hay conflictos
        # que reportar, la auditoría no se cae por esto.
        robots_conflicts: list[str] = []
        if sitemap_urls:
            try:
                from backend.collectors.crawler import USER_AGENT, _load_robots

                robots = _load_robots(home_url)
                robots_conflicts = find_robots_sitemap_conflicts(sitemap_urls, robots, USER_AGENT)
            except Exception as exc:  # noqa: BLE001 - S3: sin robots.txt legible, seguimos sin este chequeo
                import logging

                logging.getLogger(__name__).warning("No se pudo leer robots.txt para el chequeo de sitemap: %s", exc)

        found_issues = [
            *build_coverage_issues(orphans, broken, redirects, diff),
            *build_robots_sitemap_conflict_issues(robots_conflicts),
            *build_internal_link_issues(links),
            *build_duplicate_issues(dup_titles, dup_metas, dup_h1s, thin),
            *build_schema_issues(schema_validation),
            *build_cache_headers_issues(cache_analysis),
        ]

        raw_data = {
            "coverage": {
                "counts": diff["counts"],
                "orphans": orphans,
                "broken": broken,
                "redirects": redirects,
                "in_sitemap_not_crawled": diff["in_sitemap_not_crawled"],
                "crawled_not_in_sitemap": diff["crawled_not_in_sitemap"],
                "sitemap_not_indexed": diff["sitemap_not_indexed"],
                "sitemap_not_inspected": diff["sitemap_not_inspected"],
                "indexed_not_in_sitemap": diff["indexed_not_in_sitemap"],
                "robots_sitemap_conflicts": robots_conflicts,
            },
            "internal_links": {"weak": links["weak"], "deep": links["deep"], "per_page": links["per_page"][:200]},
            "duplicates": {
                "titles": dup_titles, "metas": dup_metas, "h1s": dup_h1s, "thin": thin,
            },
            "schema_validation": schema_validation,
            "cache_headers": cache_analysis,
        }

        snapshot_id = conn.execute(
            insert(snapshots).values(
                project_id=project_id, collector="site_health", status="ok",
                started_at=now, finished_at=now_iso(), raw_data=raw_data, created_at=now_iso(),
            )
        ).inserted_primary_key[0]

        created = 0
        by_severity = {"critical": 0, "high": 0, "medium": 0}
        for issue in found_issues:
            if record_issue(
                conn, project_id=project_id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now_iso()
            ):
                created += 1
                by_severity[issue.severity] += 1

        resolved = reconcile_project_issues(
            conn,
            project_id=project_id,
            owned_categories=SITE_HEALTH_CATEGORIES,
            fresh_keys={(i.category, i.title) for i in found_issues},
            now=now_iso(),
        )

    return {
        "snapshot_id": snapshot_id,
        "status": "ok",
        "issues_created": created,
        "issues_resolved": resolved,
        "by_severity": by_severity,
        "counts": diff["counts"],
        "orphans": len(orphans),
        "broken": len(broken),
        "duplicate_titles": len(dup_titles),
        "thin_pages": len(thin),
    }
