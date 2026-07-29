"""Oportunidades derivadas de GSC ya cargado (§9 Fase 1): zero-impression
pruning y keywords top-10 con CTR 0%. No requieren ninguna API nueva.

Orquesta además todos los analyzers de Fase 0-1 hacia la tabla `issues` con
su propio snapshot, para que el Action Plan tenga una sola fuente de verdad.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import desc, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.analyzers.cannibalization import QueryPageRow, detect_cannibalization
from backend.analyzers.content import detect_content_decay
from backend.analyzers.ctr import analyze_ctr, build_ctr_issues
from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.analyzers.mago import MagoIssue
from backend.analyzers.technical import analyze_page, page_data_from_stored_row
from backend.db.database import get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import gsc_daily, gsc_queries, pages, scores, snapshots

MIN_IMPRESSIONS_FOR_CTR_SIGNAL = 5

# Categorías que run_opportunities_analysis recalcula por completo cada corrida
# (registradas con page_id=None) — se reconcilian a nivel proyecto al terminar.
# "meta" aquí es el CTR-0 de top-10; las "meta" del crawler llevan page_id
# no-nulo, así que el scope page_id IS NULL de la reconciliación no las toca.
OPPORTUNITIES_OWNED_CATEGORIES = {"cannibalization", "pruning", "decay", "meta", "ctr"}

# Etiquetas humanas de cada `kind` en la tabla `scores` — fuente única para
# reports.py (deltas del reporte) y routes_dashboard.py (comparador de
# auditorías), evita mantener el mismo diccionario en dos lugares.
SCORE_KINDS_LABELS = {
    "seo": "SEO Score",
    "geo": "GEO Score",
    "technical": "Score Técnico",
    "content": "Score de Contenido",
    "local": "Local Score",
}


def detect_top10_ctr_zero(rows: list[QueryPageRow], min_impressions: int = MIN_IMPRESSIONS_FOR_CTR_SIGNAL) -> list[MagoIssue]:
    """Keywords en posición top-10 con 0 clics: síntoma de meta description que
    no convence el clic aunque Google ya la muestre arriba."""
    issues_out: list[MagoIssue] = []
    for r in rows:
        if r.position <= 10 and r.clicks == 0 and r.impressions >= min_impressions:
            issues_out.append(
                MagoIssue(
                    severity="high",
                    category="meta",
                    title=f"'{r.query}': Pos {r.position:.1f}, {r.impressions} impresiones, 0 clics",
                    page_url=r.page or None,
                    effort="5min",
                    impact=5,
                )
            )
    return issues_out


def detect_zero_impression_pages(
    crawled_pages: list[dict], impressions_by_page: dict[str, int]
) -> list[MagoIssue]:
    """Páginas indexables crawleadas sin impresiones en la ventana de GSC cargada.

    Nota de honestidad de datos (regla P1 y § #7): esto refleja SOLO lo que hay
    cargado en gsc_queries — que suele ser una ventana parcial (28d), no los 90d
    completos de Search Console. "Sin datos en esta ventana" NO es lo mismo que
    "sin tráfico confirmado": una página puede tener impresiones fuera de la
    ventana. Por eso esto es un aviso de baja prioridad para REVISAR en Search
    Console, nunca una recomendación directa de noindex/pruning.
    """
    issues_out: list[MagoIssue] = []
    for page in crawled_pages:
        if not page.get("is_indexable", True):
            continue
        impressions = impressions_by_page.get(page["url"], 0)
        if impressions == 0:
            issues_out.append(
                MagoIssue(
                    # 'medium' es el tier más bajo existente — NO infla CRÍTICO/ALTA
                    # (la queja de §#7). impact=1 lo manda al fondo del Action Plan.
                    severity="medium",
                    category="pruning",
                    title=f"{page['url']}: sin impresiones en la ventana GSC cargada (revisar, no es 'sin tráfico' confirmado)",
                    page_url=page["url"],
                    suggested=(
                        "Verifica en Search Console con la ventana completa de 90d. "
                        "Solo si ahí TAMBIÉN sale con 0 impresiones/tráfico, considera "
                        "mejorar contenido/enlaces internos o consolidar/noindex. No "
                        "actúes solo con la ventana parcial cargada aquí."
                    ),
                    effort="1h",
                    impact=1,
                )
            )
    return issues_out


def calculate_technical_score(page_rows: list[dict]) -> int | None:
    """% de celdas en verde del semáforo técnico, promediado entre todas las
    páginas crawleadas. None si aún no hay páginas (regla P1: no inventar)."""
    if not page_rows:
        return None
    total = 0
    green = 0
    for row in page_rows:
        semaphore_row = analyze_page(page_data_from_stored_row(row)).row
        for value in semaphore_row.values():
            total += 1
            if value == "green":
                green += 1
    return round(100 * green / total) if total else None


def calculate_content_score(page_rows: list[dict]) -> int | None:
    """Promedio del eeat_score (0-100, ya calculado por page en el crawler)
    entre las páginas que lo tienen. None si aún no hay datos (regla P1)."""
    values = [row["eeat_score"] for row in page_rows if row.get("eeat_score") is not None]
    if not values:
        return None
    return round(sum(values) / len(values))


def calculate_seo_score(
    technical_score: int | None,
    geo_score: int | None,
    *,
    content_score: int | None = None,
    local_score: int | None = None,
) -> tuple[int | None, dict]:
    """SEO Score combinado: promedio simple de los componentes disponibles.

    Cada componente (técnico, contenido, GEO, local) se excluye del promedio
    si no hay datos todavía — nunca se rellena con 0 (regla P1). El componente
    "autoridad" (Moz) se retiró el 2026-07-18: la credencial disponible era de
    un producto que Moz marcó como deprecado (`x-accessid: DEPRECATED` en la
    respuesta real de la API) y no hay otra fuente gratuita de Domain
    Authority. None si ningún componente tiene datos aún."""
    components = {
        k: v
        for k, v in [
            ("technical", technical_score),
            ("content", content_score),
            ("geo", geo_score),
            ("local", local_score),
        ]
        if v is not None
    }
    if not components:
        return None, {}
    return round(sum(components.values()) / len(components)), components


def _upsert_score(conn, project_id: int, date: str, kind: str, value: int, breakdown: dict) -> None:
    """Idempotente por (project_id, date, kind): corre el análisis 2 veces el
    mismo día y no duplica el punto histórico (regla S5)."""
    stmt = sqlite_insert(scores).values(
        project_id=project_id, date=date, kind=kind, value=value, breakdown=breakdown
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "date", "kind"], set_={"value": value, "breakdown": breakdown}
    )
    conn.execute(stmt)


def run_opportunities_analysis(project_id: int) -> dict:
    """Corre cannibalización + zero-impression + CTR-0 sobre los datos ya
    cargados de este proyecto y persiste los issues encontrados con un
    snapshot propio (collector='opportunities')."""
    now = now_iso()
    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, project_id)
        query_rows = conn.execute(
            select(gsc_queries).where(gsc_queries.c.project_id == project_id, gsc_queries.c.date == latest_date)
        ).all()
        page_rows = conn.execute(
            select(pages).where(pages.c.project_id == project_id)
        ).all()
        daily_rows = conn.execute(
            select(gsc_daily.c.date, gsc_daily.c.clicks).where(gsc_daily.c.project_id == project_id)
        ).all()

        qp_rows = [
            QueryPageRow(r.query, r.page or "", r.clicks, r.impressions, r.position)
            for r in query_rows
        ]

        impressions_by_page: dict[str, int] = defaultdict(int)
        for r in query_rows:
            if r.page:
                impressions_by_page[r.page] += r.impressions

        # Mapa de redirects: dos URLs de GSC que terminan en el mismo destino
        # son UNA página con una entrada vieja, no canibalización. Se combina
        # la tabla `pages` PERSISTIDA (acumula cobertura de todos los crawls
        # anteriores) con el crawl más reciente — bug real 2026-07-24: usar
        # SOLO el último crawl perdía las URLs viejas que ya nadie enlaza y no
        # se re-visitan, así que su redirect se "olvidaba" entre corridas.
        from backend.analyzers.coverage import build_redirect_map

        persisted_pages = [
            {"url": r.url, "redirected_to": r.redirected_to}
            for r in conn.execute(select(pages.c.url, pages.c.redirected_to).where(pages.c.project_id == project_id)).all()
        ]
        redirect_map = build_redirect_map(persisted_pages)

        crawler_snap = conn.execute(
            select(snapshots.c.raw_data)
            .where(
                snapshots.c.project_id == project_id,
                snapshots.c.collector == "crawler",
                snapshots.c.status.in_(["ok", "partial"]),
            )
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()
        if crawler_snap and crawler_snap[0]:
            redirect_map = {**redirect_map, **build_redirect_map((crawler_snap[0] or {}).get("pages", []))}

        # El mapa del crawler solo cubre lo que se visitó. Las URLs de GSC que
        # nunca crawleamos son justo las que producen canibalizaciones falsas
        # (caso real: /reparacion-macbook-sevilla-valle →308→
        # /reparacion-macbook/sevilla-valle). Resolvemos por red SOLO las
        # candidatas — queries donde 2+ URLs distintas compiten — y acotado.
        from backend.analyzers.coverage import canonical_url as _canon

        by_query_pages: dict[str, set[str]] = defaultdict(set)
        for r in qp_rows:
            if r.page:
                by_query_pages[r.query].add(r.page)
        candidate_urls = sorted({u for pages_ in by_query_pages.values() if len({_canon(p) for p in pages_}) > 1 for u in pages_})
        if candidate_urls:
            try:
                from backend.collectors.redirects import resolve_redirect_targets

                redirect_map = {**redirect_map, **resolve_redirect_targets(candidate_urls, redirect_map)}
            except Exception as exc:  # noqa: BLE001 - S3: sin resolución seguimos con el mapa del crawler
                import logging

                logging.getLogger(__name__).warning("Resolución de redirects falló: %s", exc)

        found_issues: list[MagoIssue] = []
        found_issues.extend(detect_cannibalization(qp_rows, redirect_map))
        found_issues.extend(detect_top10_ctr_zero(qp_rows))
        found_issues.extend(
            detect_zero_impression_pages([dict(p._mapping) for p in page_rows], impressions_by_page)
        )

        decay_reason, decay_issue = detect_content_decay(
            [{"date": r.date, "clicks": r.clicks} for r in daily_rows]
        )
        if decay_issue:
            found_issues.append(decay_issue)

        # CTR contra el propio baseline del sitio (§ mejoras 2026-07-26). No usa
        # curvas de industria: el veredicto sale de los datos de GSC del propio
        # proyecto, y si no hay clics suficientes lo declara en vez de opinar.
        ctr_analysis = analyze_ctr([dict(r._mapping) for r in query_rows])
        found_issues.extend(build_ctr_issues(ctr_analysis))

        page_dicts = [dict(p._mapping) for p in page_rows]
        technical_score = calculate_technical_score(page_dicts)
        content_score = calculate_content_score(page_dicts)

        def _latest(kind: str) -> int | None:
            row = conn.execute(
                select(scores.c.value)
                .where(scores.c.project_id == project_id, scores.c.kind == kind)
                .order_by(desc(scores.c.date))
                .limit(1)
            ).first()
            return row[0] if row else None

        geo_score = _latest("geo")
        local_score = _latest("local")
        seo_score, seo_components = calculate_seo_score(
            technical_score,
            geo_score,
            content_score=content_score,
            local_score=local_score,
        )

        today = now[:10]
        if technical_score is not None:
            _upsert_score(conn, project_id, today, "technical", technical_score, {})
        if content_score is not None:
            _upsert_score(conn, project_id, today, "content", content_score, {})
        if seo_score is not None:
            _upsert_score(conn, project_id, today, "seo", seo_score, seo_components)

        snapshot_id = conn.execute(
            insert(snapshots).values(
                project_id=project_id,
                collector="opportunities",
                status="ok",
                started_at=now,
                finished_at=now_iso(),
                raw_data={"issues_found": len(found_issues), "ctr": ctr_analysis},
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]

        created = 0
        by_severity = {"critical": 0, "high": 0, "medium": 0}
        for issue in found_issues:
            was_created = record_issue(
                conn,
                project_id=project_id,
                snapshot_id=snapshot_id,
                page_id=None,
                issue=issue,
                now=now_iso(),
            )
            if was_created:
                created += 1
                by_severity[issue.severity] += 1

        # Reconciliación de las categorías que ESTE analyzer recalcula por
        # completo cada corrida: cierra las viejas que ya no aparecen (evita
        # fantasmas y duplicados al cambiar una regla, ej. el reformulado de
        # pruning). Scoped a page_id IS NULL para no tocar las del crawler.
        resolved = reconcile_project_issues(
            conn,
            project_id=project_id,
            owned_categories=OPPORTUNITIES_OWNED_CATEGORIES,
            fresh_keys={(i.category, i.title) for i in found_issues},
            now=now_iso(),
        )

    return {
        "snapshot_id": snapshot_id,
        "issues_created": created,
        "issues_resolved": resolved,
        "by_severity": by_severity,
        "decay_status": decay_reason or ("caída detectada" if decay_issue else "sin caída significativa"),
    }
