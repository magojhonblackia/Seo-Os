"""Persistencia idempotente de issues (regla S5 aplicada al Action Plan).

Sin esto, correr el crawler o el análisis de oportunidades dos veces duplica
cada hallazgo en la tabla `issues` cada vez, inflando artificialmente
issues_open y el Action Plan. Se deduplica por (project_id, category, title)
mientras el issue siga "open": mismo problema detectado de nuevo no crea una
fila nueva; un issue ya marcado done/dismissed sí puede reabrirse si reaparece.
"""
from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from backend.analyzers.mago import MagoIssue
from backend.db.schema import issues

# Categorías cuyos issues nacen del análisis POR PÁGINA del crawler
# (technical.analyze_page + content.build_content_issues). La reconciliación
# solo cierra estas — nunca toca canibalización/pruning/local/backlinks, que
# se registran con page_id=None desde otros analyzers.
CRAWLER_OWNED_CATEGORIES = frozenset(
    {"meta", "h1", "schema", "og", "canonical", "index", "content", "zombie", "eeat", "decay"}
)


def record_issue(
    conn: Connection,
    *,
    project_id: int,
    snapshot_id: int,
    page_id: int | None,
    issue: MagoIssue,
    now: str,
) -> bool:
    """Inserta el issue si no existe ya uno abierto igual. Devuelve True si insertó."""
    existing = conn.execute(
        select(issues.c.id).where(
            issues.c.project_id == project_id,
            issues.c.category == issue.category,
            issues.c.title == issue.title,
            issues.c.status == "open",
        )
    ).first()
    if existing:
        return False

    conn.execute(
        insert(issues).values(
            project_id=project_id,
            page_id=page_id,
            snapshot_id=snapshot_id,
            severity=issue.severity,
            category=issue.category,
            title=issue.title,
            current_text=issue.current,
            suggested_text=issue.suggested,
            effort=issue.effort,
            impact=issue.impact,
            status="open",
            detected_at=now,
        )
    )
    return True


def reconcile_page_issues(
    conn: Connection,
    *,
    project_id: int,
    page_id: int,
    fresh_keys: set[tuple[str, str]],
    now: str,
) -> int:
    """Cierra las issues abiertas de ESTA página (categorías del crawler) que el
    análisis fresco ya NO reporta — status='resolved', con resolved_at.

    Sin esto, un falso positivo corregido (o un problema arreglado en el sitio
    real) quedaba 'open' para siempre, ensuciando el reporte (§ falsos positivos
    recurrentes). 'resolved' no es 'done' (lo arregló el usuario) ni 'dismissed'
    (lo descartó a mano): es "el detector dejó de verlo al re-analizar". Como
    todos los filtros del reporte usan status=='open', estos salen del conteo.
    Devuelve cuántas cerró. Solo toca page_id no-nulo (exclusivo del crawler)."""
    rows = conn.execute(
        select(issues.c.id, issues.c.category, issues.c.title).where(
            issues.c.project_id == project_id,
            issues.c.page_id == page_id,
            issues.c.status == "open",
            issues.c.category.in_(CRAWLER_OWNED_CATEGORIES),
        )
    ).all()
    closed = 0
    for row in rows:
        if (row.category, row.title) not in fresh_keys:
            conn.execute(
                update(issues).where(issues.c.id == row.id).values(status="resolved", resolved_at=now)
            )
            closed += 1
    return closed


def reconcile_project_issues(
    conn: Connection,
    *,
    project_id: int,
    owned_categories: set[str],
    fresh_keys: set[tuple[str, str]],
    now: str,
) -> int:
    """Igual que reconcile_page_issues pero a nivel PROYECTO, para los analyzers
    que recalculan su categoría entera en cada corrida y registran con
    page_id=None (opportunities: canibalización/pruning/decay/meta-CTR). Solo
    toca issues con page_id NULL en las categorías dadas — nunca las del crawler
    (page_id no-nulo). Sin esto, reformular o corregir una de esas reglas dejaba
    el issue viejo 'open' o creaba un duplicado con título nuevo."""
    rows = conn.execute(
        select(issues.c.id, issues.c.category, issues.c.title).where(
            issues.c.project_id == project_id,
            issues.c.page_id.is_(None),
            issues.c.status == "open",
            issues.c.category.in_(owned_categories),
        )
    ).all()
    closed = 0
    for row in rows:
        if (row.category, row.title) not in fresh_keys:
            conn.execute(
                update(issues).where(issues.c.id == row.id).values(status="resolved", resolved_at=now)
            )
            closed += 1
    return closed
