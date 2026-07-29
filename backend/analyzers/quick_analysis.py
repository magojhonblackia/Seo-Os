"""Análisis rápido ad-hoc de una URL arbitraria: una sola página, sin
persistir nada (no es un proyecto ni un competidor registrado). Reutiliza
los mismos analyzers deterministas que el resto de la plataforma
(technical.py, geo.py) para que el reporte sea consistente con el dashboard.

Diferencia deliberada de política de robots.txt vs. crawler.py: aquí es UNA
sola página que el usuario pidió explícitamente analizar (no un crawl
autónomo multi-página), así que si el fetch de robots.txt falla por completo
(red, timeout) SE PROCEDE con el análisis de esa única página en vez de
bloquear por defecto — el riesgo de una sola petición GET puntual es mínimo
comparado con un crawl completo. Si robots.txt sí responde (200 o 404) y
prohíbe explícitamente la URL, se respeta y se bloquea.
"""
from __future__ import annotations

from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from backend.analyzers.geo import build_ai_crawler_matrix, calculate_geo_score
from backend.analyzers.technical import PageData, analyze_page
from backend.analyzers.url_safety import UnsafeURLError, validate_public_url
from backend.collectors.crawler import (
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _extract_page_data,
    _normalize_url,
)


class QuickAnalysisError(Exception):
    """Error legible para el usuario (regla S3): SSRF bloqueado, robots.txt
    prohíbe la URL, fetch falló, contenido no es HTML, etc."""


def run_quick_analysis(url: str) -> dict:
    try:
        safe_url = validate_public_url(url)
    except UnsafeURLError as exc:
        raise QuickAnalysisError(str(exc)) from exc

    normalized_url = _normalize_url(safe_url)

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        robots_content = ""
        robots_fetch_failed = False
        try:
            robots_resp = client.get(urljoin(normalized_url, "/robots.txt"))
            if robots_resp.status_code == 200:
                robots_content = robots_resp.text
            # 404 u otro no-200: se trata como "sin restricciones" (contenido vacío)
        except httpx.HTTPError:
            robots_fetch_failed = True

        if not robots_fetch_failed:
            robots = RobotFileParser()
            robots.parse(robots_content.splitlines())
            if not robots.can_fetch(USER_AGENT, normalized_url):
                raise QuickAnalysisError("robots.txt de este sitio prohíbe analizar esta URL")

        try:
            response = client.get(normalized_url)
        except httpx.HTTPError as exc:
            raise QuickAnalysisError(f"No se pudo obtener la página: {exc}") from exc

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            raise QuickAnalysisError(f"La URL no devuelve HTML (content-type: {content_type or 'desconocido'})")
        if len(response.content) > MAX_BODY_BYTES:
            raise QuickAnalysisError("La página es demasiado grande para analizar (límite 2MB)")

        page = _extract_page_data(normalized_url, response.status_code, response.text)

        llms_exists = False
        try:
            llms_response = client.get(urljoin(normalized_url, "/llms.txt"))
            llms_exists = llms_response.status_code == 200
        except httpx.HTTPError:
            pass

    page_data = PageData(
        url=page.url,
        title=page.title,
        meta_description=page.meta_description,
        h1_tags=page.h1_tags,
        schema_types=page.schema_types,
        schema_has_errors=page.schema_has_errors,
        og=page.og,
        canonical=page.canonical,
        is_indexable=page.is_indexable,
    )
    report = analyze_page(page_data)

    matrix = build_ai_crawler_matrix(robots_content)
    geo_score, _components = calculate_geo_score(llms_exists, False, matrix)

    return {
        "url": page.url,
        "title": page.title,
        "meta_description": page.meta_description,
        "h1_tags": page.h1_tags,
        "schema_types": page.schema_types,
        "og": page.og,
        "canonical": page.canonical,
        "is_indexable": page.is_indexable,
        "word_count": page.word_count,
        "technical_row": report.row,
        "issues": [issue.to_dict() for issue in report.issues],
        "geo": {"llms_txt_exists": llms_exists, "geo_score": geo_score, "matrix": matrix},
    }
