"""Gap real de keywords contra un competidor: qué keywords rankea de verdad
(datos de DataForSEO Labs, no una comparación de texto de título/H1 como
`keyword_gap()` en competitors.py) que nosotros no tenemos ninguna
impresión real en Search Console (§ mejoras 2026-08-28).

Honestidad (P1): "no tenemos impresiones reales" es distinto de "no
rankeamos" — puede que rankeemos y Search Console aún no lo haya reportado
(latencia de 2-3 días), o que la keyword nunca haya generado impresiones
medibles. Se declara así, nunca como certeza de que no rankeamos."""
from __future__ import annotations

import unicodedata

from backend.analyzers.mago import MagoIssue

# Por debajo de esto el volumen es tan bajo que priorizarlo no vale el
# esfuerzo — mismo criterio de "no opinar sin evidencia que sostenga la
# prioridad" que el resto del proyecto (ver MIN_IMPRESSIONS_KEYWORD en ctr.py).
MIN_SEARCH_VOLUME_TO_FLAG = 10


def _normalize(text: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return stripped.lower().strip()


def find_keyword_gaps(competitor_keywords: list[dict], our_keywords: set[str]) -> list[dict]:
    """`competitor_keywords`: salida de _fetch_ranked_keywords (keyword/position/
    search_volume/url). `our_keywords`: keywords reales que YA tenemos
    registradas (ej. de gsc_queries) — comparación case/acento-insensible."""
    our_normalized = {_normalize(k) for k in our_keywords}
    gaps = [
        kw
        for kw in competitor_keywords
        if _normalize(kw["keyword"]) not in our_normalized
        and (kw.get("search_volume") or 0) >= MIN_SEARCH_VOLUME_TO_FLAG
    ]
    gaps.sort(key=lambda k: (-(k.get("search_volume") or 0), k.get("position") if k.get("position") is not None else 999))
    return gaps


def build_competitor_keyword_issues(domain: str, gaps: list[dict]) -> list[MagoIssue]:
    if not gaps:
        return []
    sample = ", ".join(
        f'"{g["keyword"]}" (pos {g.get("position", "?")}, {g.get("search_volume", "?")} búsquedas/mes)'
        for g in gaps[:5]
    )
    return [
        MagoIssue(
            severity="medium",
            category="competitor_keywords",
            title=(
                f"{domain} rankea {len(gaps)} keyword(s) real(es) con volumen que tú no tienes "
                "ninguna impresión registrada"
            ),
            current=sample,
            suggested=(
                "Son keywords donde el competidor SÍ rankea de verdad (dato real de DataForSEO, no una "
                "suposición de tema/título) y tú no tienes ninguna impresión en Search Console. Evalúa cuáles "
                "reflejan un servicio que de verdad ofreces antes de crear contenido — no todas valen la pena, "
                "y 'sin impresiones' no es lo mismo que 'no rankeas'."
            ),
            effort="1d",
            impact=3,
        )
    ]
