"""Compresión (Content-Encoding) y presencia de Cache-Control (§ herramientas
de mercado 2026-07-24, adaptado de cache_compression_checker.py).

Alcance deliberadamente conservador (P1): SOLO se marca la AUSENCIA total de
compresión o de Cache-Control — nunca se juzga el VALOR de max-age. Verificado
en jcreparaciones.com: usa `Cache-Control: public, max-age=0, must-revalidate`,
que es el patrón CORRECTO para páginas Next.js/ISR (el CDN revalida en cada
visita) — un checker que exigiera "max-age alto" habría marcado esto como un
falso problema. La ausencia total de Cache-Control sí es inequívocamente mala
en cualquier arquitectura: sin el header, el navegador aplica caché heurística
(RFC 7234) — el mismo bug de fondo que encontramos y arreglamos en nuestra
propia API esta sesión.

Como estos headers casi siempre son configuración de servidor/CDN idéntica en
todo el sitio (verificado: home y una página interna dieron los mismos
valores), se agrega un resumen — no un issue por página."""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue


def analyze_cache_headers(crawled_pages: list[dict]) -> dict:
    checked = [p for p in crawled_pages if p.get("status_code") == 200]
    if not checked:
        return {"pages_checked": 0, "uncompressed": [], "no_cache_control": []}

    uncompressed = [p["url"] for p in checked if not p.get("content_encoding")]
    no_cache_control = [p["url"] for p in checked if not p.get("cache_control")]
    return {
        "pages_checked": len(checked),
        "uncompressed": sorted(uncompressed),
        "no_cache_control": sorted(no_cache_control),
    }


def build_cache_headers_issues(analysis: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    total = analysis.get("pages_checked", 0)
    if not total:
        return issues

    uncompressed = analysis.get("uncompressed", [])
    # Solo se reporta si afecta una fracción significativa — un par de páginas
    # sin comprimir puede ser un caso puntual (respuesta de error, redirect
    # intermedio); si afecta a casi todo el sitio, es config de servidor real.
    if len(uncompressed) >= max(1, total // 2):
        issues.append(
            MagoIssue(
                severity="medium",
                category="performance",
                title=f"{len(uncompressed)} de {total} páginas se sirven SIN compresión (gzip/brotli)",
                current=", ".join(uncompressed[:5]),
                suggested="Activa compresión gzip o brotli en el servidor/CDN — reduce el peso transferido sin cambiar el contenido, ayuda directamente al LCP.",
                effort="1h",
                impact=2,
            )
        )

    no_cache = analysis.get("no_cache_control", [])
    if len(no_cache) >= max(1, total // 2):
        issues.append(
            MagoIssue(
                severity="medium",
                category="performance",
                title=f"{len(no_cache)} de {total} páginas se sirven SIN header Cache-Control",
                current=", ".join(no_cache[:5]),
                suggested="Agrega Cache-Control (aunque sea 'max-age=0, must-revalidate' para HTML dinámico) — sin el header, el navegador aplica caché heurística impredecible en vez de una regla explícita.",
                effort="1h",
                impact=2,
            )
        )
    return issues
