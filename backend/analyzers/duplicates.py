"""Duplicados (title/meta/H1 repetidos) y contenido thin (§ herramientas nuevas
2026-07-23). 100% sobre la tabla `pages` ya poblada — cero requests nuevas.

Por qué hace falta: títulos y metas duplicados son una de las causas más comunes
de que Google elija mal qué página mostrar (o no muestre ninguna). Es
especialmente probable en sitios con páginas programáticas (ej.
/reparacion/iphone/<modelo>/pantalla) donde el template repite el mismo patrón.
Contenido thin (pocas palabras) es señal de página de bajo valor que puede
arrastrar la calidad percibida del dominio.

Funciones puras y testeables. P1: no se inventa nada; una página sin title
simplemente no entra en el grupo de 'title duplicado'."""
from __future__ import annotations

import re

from backend.analyzers.mago import MagoIssue

THIN_CONTENT_WORDS = 200


def _norm_text(value: str | None) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip().lower()
    return collapsed or None


def find_duplicate_field(
    pages: list[dict], field: str, redirect_map: dict[str, str] | None = None
) -> list[dict]:
    """Agrupa páginas indexables por el valor NORMALIZADO de un campo
    (title/meta_description/h1) y devuelve los grupos con 2+ páginas — el
    duplicado real que confunde a Google sobre cuál mostrar.

    `redirect_map` es esencial para no mentir: una URL que redirige (301/308) NO
    es una página aparte. Como el crawler sigue la redirección y guarda el
    contenido del destino bajo la URL de origen, sin este filtro los alias
    aparecen como "título duplicado". Verificado en jcreparaciones.com: el 100%
    de los duplicados detectados eran alias de redirect, no duplicados reales.
    """
    from backend.analyzers.coverage import is_redirecting

    groups: dict[str, list[str]] = {}
    for page in pages:
        if not page.get("is_indexable", True):
            continue
        if page.get("status_code") not in (None, 200):
            continue
        if is_redirecting(page["url"], redirect_map):
            continue  # alias de redirect: es la misma página, no un duplicado
        norm = _norm_text(page.get(field))
        if norm is None:
            continue
        groups.setdefault(norm, []).append(page["url"])

    dups = [
        {"value": value, "urls": sorted(urls), "count": len(urls)}
        for value, urls in groups.items()
        if len(urls) > 1
    ]
    dups.sort(key=lambda d: d["count"], reverse=True)
    return dups


def find_thin_content(
    pages: list[dict], threshold: int = THIN_CONTENT_WORDS, redirect_map: dict[str, str] | None = None
) -> list[dict]:
    """Páginas indexables (200) con menos de `threshold` palabras — candidatas a
    thin content. Se excluyen las que no tienen word_count medido (P1: no se
    asume 0) y los alias de redirect (no son páginas propias)."""
    from backend.analyzers.coverage import is_redirecting

    thin = []
    for page in pages:
        if not page.get("is_indexable", True):
            continue
        if page.get("status_code") not in (None, 200):
            continue
        if is_redirecting(page["url"], redirect_map):
            continue
        wc = page.get("word_count")
        if isinstance(wc, int) and wc < threshold:
            thin.append({"url": page["url"], "word_count": wc})
    thin.sort(key=lambda r: r["word_count"])
    return thin


def build_duplicate_issues(
    dup_titles: list[dict],
    dup_metas: list[dict],
    dup_h1s: list[dict],
    thin: list[dict],
) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    def _affected(groups: list[dict]) -> int:
        return sum(g["count"] for g in groups)

    if dup_titles:
        issues.append(
            MagoIssue(
                severity="high",
                category="duplicates",
                title=f"{_affected(dup_titles)} páginas con TITLE duplicado ({len(dup_titles)} título(s) repetido(s))",
                current="; ".join(f'"{g["value"][:50]}" ×{g["count"]}' for g in dup_titles[:3]),
                suggested="Haz cada title único (incluye el modelo/ciudad/variante específica) — con títulos iguales Google no sabe cuál página mostrar y suele mostrar peor.",
                effort="1h",
                impact=4,
            )
        )

    if dup_metas:
        issues.append(
            MagoIssue(
                severity="medium",
                category="duplicates",
                title=f"{_affected(dup_metas)} páginas con META DESCRIPTION duplicada ({len(dup_metas)} repetida(s))",
                current="; ".join(f'"{g["value"][:50]}" ×{g["count"]}' for g in dup_metas[:3]),
                suggested="Escribe una meta description única por página con su CTA propio — la duplicada baja el CTR y desperdicia el espacio en el resultado.",
                effort="1h",
                impact=3,
            )
        )

    if dup_h1s:
        issues.append(
            MagoIssue(
                severity="medium",
                category="duplicates",
                title=f"{_affected(dup_h1s)} páginas con H1 duplicado ({len(dup_h1s)} repetido(s))",
                current="; ".join(f'"{g["value"][:50]}" ×{g["count"]}' for g in dup_h1s[:3]),
                suggested="Diferencia el H1 de cada página con su tema específico.",
                effort="1h",
                impact=2,
            )
        )

    if thin:
        issues.append(
            MagoIssue(
                severity="medium",
                category="duplicates",
                title=f"{len(thin)} página(s) con contenido thin (< {THIN_CONTENT_WORDS} palabras)",
                current=", ".join(f"{t['url']} ({t['word_count']}p)" for t in thin[:5]),
                suggested="Amplía el contenido con información útil y específica (precios, tiempos, garantía, FAQ) o consolida varias thin en una — el contenido escaso rankea peor y baja la calidad percibida del dominio.",
                effort="1d",
                impact=3,
            )
        )

    return issues
