"""Detección de canibalización de keywords (§9 Fase 1).

Señal medible directamente desde gsc_queries ya cargado — no requiere ninguna
API nueva: cuando varias páginas del mismo proyecto compiten por la misma
query, Google reparte autoridad entre ellas y ninguna llega a la cima.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

from backend.analyzers.mago import MagoIssue


@dataclass
class QueryPageRow:
    query: str
    page: str
    clicks: int
    impressions: int
    position: float


def _canonical_page(url: str) -> str:
    """Normaliza una URL para agrupar canibalización SIN marcar duplicados
    estructurales (§ #5a): unifica http/https, quita 'www.' del host, y quita
    el slash final (salvo raíz). Así 'https://www.sitio.com/x/' y
    'http://sitio.com/x' cuentan como la MISMA página — hay un 301 entre ellas,
    no dos páginas compitiendo.

    Para rutas distintas unidas por un 301/308 (ej. '/a-b-c' vs '/a-b/c') se usa
    el `redirect_map` que construye coverage.py con lo que el crawler YA
    observó — sin peticiones nuevas. Ver detect_cannibalization."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}"


def detect_cannibalization(
    rows: list[QueryPageRow], redirect_map: dict[str, str] | None = None
) -> list[MagoIssue]:
    """`redirect_map` (de coverage.build_redirect_map) resuelve la cadena de
    301/308 antes de agrupar: dos URLs que terminan en el MISMO destino son una
    sola página con una entrada vieja, no dos compitiendo. Sin esto, cada
    redirect del sitio genera una canibalización falsa."""
    from backend.analyzers.coverage import resolve_redirect

    by_query: dict[str, list[QueryPageRow]] = defaultdict(list)
    for r in rows:
        if r.page:  # filas sin page (agregado a nivel de site) no cuentan
            by_query[r.query].append(r)

    issues: list[MagoIssue] = []
    for query, entries in by_query.items():
        # Mejor posición por DESTINO FINAL — así www/non-www (§ #5a) y las
        # rutas unidas por un 301/308 cuentan como una sola página, tanto para
        # decidir si hay canibalización como para la severidad.
        best_by_canonical: dict[str, QueryPageRow] = {}
        for e in entries:
            canon = resolve_redirect(e.page, redirect_map) if redirect_map else _canonical_page(e.page)
            if canon not in best_by_canonical or e.position < best_by_canonical[canon].position:
                best_by_canonical[canon] = e
        if len(best_by_canonical) < 2:
            continue

        best = min(best_by_canonical.values(), key=lambda e: e.position)
        total_impressions = sum(e.impressions for e in entries)
        pages_list = ", ".join(sorted(best_by_canonical))

        # Si 2+ páginas (canónicas) rankean ambas en el top 20, la
        # canibalización es más dañina: Google reparte autoridad y ninguna
        # termina de despegar.
        top20_count = sum(1 for e in best_by_canonical.values() if e.position <= 20)
        severity = "critical" if top20_count >= 2 else "high"

        issues.append(
            MagoIssue(
                severity=severity,
                category="cannibalization",
                title=f"'{query}': {len(best_by_canonical)} páginas compitiendo ({total_impressions} impresiones combinadas)",
                current=pages_list,
                suggested=(
                    f"Consolidar en {best.page} (mejor posición: {best.position:.1f}) "
                    "y redirigir o diferenciar claramente el resto con enlaces internos"
                ),
                page_url=best.page,
                effort="1d",
                impact=4 if severity == "critical" else 3,
            )
        )
    return issues
