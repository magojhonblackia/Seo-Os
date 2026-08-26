"""Contenido casi-duplicado entre páginas generadas por la misma plantilla
(§ mejoras 2026-08-26, auditoría manual del usuario contra
jcreparaciones.com — categoría 2 del prompt de 8 problemas que Search
Console/Semrush no detectan directamente).

Por qué hace falta: un sitio de "programmatic SEO" (una plantilla + una
variable que cambia — ciudad, marca, modelo) puede tener canonical, URL y
metadata distintos por página y AUN ASÍ ser el mismo contenido a ojos de
Google si el texto visible casi no cambia. Semrush/GSC no cruzan texto entre
páginas del mismo sitio — solo lo detecta comparando el texto real.

Verificado en vivo contra jc antes de escribir esto: `/reparacion-iphone/palmira`
vs `/reparacion-iphone/tulua` — 93.5% de similitud de texto cruda (95.3% tras
neutralizar el nombre de la ciudad). Causa raíz real encontrada en el mismo
sitio: `lib/cities.ts` tenía un campo `localContext` diseñado explícitamente
para esto, pero la plantilla (`lib/renderBrandPage.tsx`) nunca lo usaba.

Agrupación de páginas por plantilla: sin conocer de antemano qué segmento de
la URL es "la variable" (ciudad, marca, modelo...), se generan todas las
firmas posibles reemplazando UN segmento a la vez por '*'. Dos URLs que
comparten una firma comparten plantilla. Deliberadamente simple (no intenta
adivinar cuál segmento es semánticamente la variable) — funciona igual de
bien para /reparacion-iphone/{ciudad} que para /reparacion/{marca}/{modelo}/{servicio}
(cada URL genera una firma por posición, así que puede pertenecer a varios
grupos distintos a la vez, uno por cada plantilla real que comparta).
"""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlparse

from backend.analyzers.mago import MagoIssue

# Con menos de esto no hay evidencia de "plantilla" real — 2 páginas parecidas
# pueden ser coincidencia, 3+ ya es un patrón.
MIN_GROUP_SIZE = 3
# Calibrado contra el caso real de jc (93.5% crudo) con margen para no ser
# demasiado agresivo con páginas que comparten boilerplate por diseño.
SIMILARITY_THRESHOLD = 0.85
# Evita blow-up O(n²) en grupos grandes (ej. 31 ciudades) — una muestra ya
# es suficiente evidencia de que la plantilla no diferencia contenido.
MAX_PAGES_PER_GROUP = 6
# Páginas con menos texto que esto no dan evidencia real de duplicado — se
# parecerían aunque no compartieran plantilla, solo por ser cortas.
MIN_TEXT_LENGTH = 200


def _path_segments(url: str) -> list[str]:
    path = urlparse(url).path.strip("/")
    return path.split("/") if path else []


def _wildcard_signatures(url: str) -> list[str]:
    """Una firma por cada posición de segmento reemplazada por '*'. Requiere
    2+ segmentos — una sola página de nivel raíz no da una plantilla real."""
    segments = _path_segments(url)
    if len(segments) < 2:
        return []
    signatures = []
    for i in range(len(segments)):
        sig = segments.copy()
        sig[i] = "*"
        signatures.append("/" + "/".join(sig))
    return signatures


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def detect_templated_duplicate_content(pages: list[dict]) -> list[dict]:
    """`pages`: lista de dicts con al menos 'url' y 'body_text_sample' (el
    formato que guarda el snapshot del crawler). Devuelve un hallazgo por
    grupo de plantilla cuya similitud de texto promedio supera el umbral —
    uno por grupo, no por página: es un problema de la plantilla entera."""
    text_by_url = {
        p["url"]: p.get("body_text_sample") or ""
        for p in pages
        if p.get("url") and len(p.get("body_text_sample") or "") >= MIN_TEXT_LENGTH
    }
    if len(text_by_url) < MIN_GROUP_SIZE:
        return []

    groups: dict[str, list[str]] = defaultdict(list)
    for url in text_by_url:
        for sig in _wildcard_signatures(url):
            groups[sig].append(url)

    findings = []
    seen_url_sets: set[frozenset] = set()
    for signature, urls in groups.items():
        if len(urls) < MIN_GROUP_SIZE:
            continue
        sample = sorted(urls)[:MAX_PAGES_PER_GROUP]
        # Evita reportar el mismo conjunto de URLs dos veces si dos firmas
        # distintas agrupan exactamente las mismas páginas.
        key = frozenset(sample)
        if key in seen_url_sets:
            continue

        ratios = []
        pairs_checked = []
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                a, b = sample[i], sample[j]
                ratio = _similarity(text_by_url[a], text_by_url[b])
                ratios.append(ratio)
                pairs_checked.append((a, b, ratio))

        if not ratios:
            continue
        avg_similarity = sum(ratios) / len(ratios)
        if avg_similarity < SIMILARITY_THRESHOLD:
            continue

        seen_url_sets.add(key)
        worst_pair = min(pairs_checked, key=lambda t: t[2])  # el par MÁS distinto igual supera el umbral
        findings.append(
            {
                "signature": signature,
                "group_size": len(urls),
                "sampled": len(sample),
                "avg_similarity": round(avg_similarity, 3),
                "example_pair": [worst_pair[0], worst_pair[1]],
                "example_pair_similarity": round(worst_pair[2], 3),
            }
        )

    return findings


def build_duplicate_content_issues(findings: list[dict]) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    for f in findings:
        pct = round(f["avg_similarity"] * 100, 1)
        issues.append(
            MagoIssue(
                severity="high",
                category="duplicate_content",
                title=(
                    f"Plantilla '{f['signature']}': {f['group_size']} página(s) con "
                    f"~{pct}% de similitud de texto entre sí"
                ),
                current=(
                    f"Ejemplo comparado: {f['example_pair'][0]} vs {f['example_pair'][1]} "
                    f"({round(f['example_pair_similarity'] * 100, 1)}% de similitud, el par MÁS distinto de la muestra)"
                ),
                suggested=(
                    "Cada página tiene canonical y metadata distintos, pero Google puede tratarlas como "
                    "contenido duplicado si el texto visible casi no cambia entre una y otra. Agrega contenido "
                    "genuinamente distinto por página (dato local/específico ya disponible pero no usado en la "
                    "plantilla, si existe, o redactado a mano para el grupo con más volumen de búsqueda real)."
                ),
                effort="1d",
                impact=3,
            )
        )
    return issues
