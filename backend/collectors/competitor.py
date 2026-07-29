"""Collector de competidores (§9 Fase 3): reutiliza el crawler propio contra
un dominio de un competidor YA REGISTRADO en `projects.competitors` (nunca
una URL arbitraria del cliente, regla §4.3).

Un competidor no es un "proyecto" propio (no tiene fila en `projects`), así
que su snapshot se guarda bajo el proyecto DUEÑO con
collector=f"competitor:{dominio}", siguiendo el mismo patrón S2 (crudo antes
de procesar) sin necesidad de cambiar el schema.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import asdict
from urllib.parse import urlparse

import httpx
from sqlalchemy import insert, select

from backend.analyzers.geo import build_ai_crawler_matrix, calculate_geo_score
from backend.analyzers.technical import PageData, analyze_page
from backend.collectors.crawler import MAX_PAGES_COMPETITOR, USER_AGENT, crawl_site
from backend.collectors.geo import REQUEST_TIMEOUT, _fetch_text, is_reachable
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots

logger = logging.getLogger(__name__)


def _aggregate_content_insights(content_pages: list) -> dict:
    """Todo lo que se puede sacar de las páginas ya crawleadas del competidor
    sin inventar nada extra (regla P1): tipos de schema que usa, señales
    E-E-A-T, longitudes promedio de title/meta, y el primer LocalBusiness
    con NAP que se encuentre (si alguno)."""
    if not content_pages:
        return {
            "schema_coverage": {}, "avg_word_count": None, "avg_title_length": None,
            "avg_meta_length": None, "eeat_signals": {}, "local_business_detected": None,
        }

    schema_counter: Counter[str] = Counter()
    word_counts, title_lengths, meta_lengths = [], [], []
    author_count = date_count = contact_count = 0
    local_business_detected = None

    for page in content_pages:
        schema_counter.update(page.schema_types)
        if page.word_count:
            word_counts.append(page.word_count)
        if page.title:
            title_lengths.append(len(page.title))
        if page.meta_description:
            meta_lengths.append(len(page.meta_description))
        author_count += int(page.has_author)
        date_count += int(page.has_date)
        contact_count += int(page.has_contact)
        if local_business_detected is None and page.local_business_schema:
            local_business_detected = page.local_business_schema

    n = len(content_pages)

    def _avg(values: list[int]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    return {
        "schema_coverage": dict(schema_counter.most_common()),
        "avg_word_count": _avg(word_counts),
        "avg_title_length": _avg(title_lengths),
        "avg_meta_length": _avg(meta_lengths),
        "eeat_signals": {
            "has_author_pct": round(100 * author_count / n),
            "has_date_pct": round(100 * date_count / n),
            "has_contact_pct": round(100 * contact_count / n),
        },
        "local_business_detected": local_business_detected,
    }


def _load_owner_project(project_slug: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if row is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")
    return dict(row._mapping)


def scan_competitor(project_slug: str, competitor_domain: str, max_pages: int | None = None) -> dict:
    """Crawlea un competidor registrado, calcula sus scores técnico/GEO y
    persiste todo como snapshot del proyecto dueño. Devuelve el resumen.
    """
    owner = _load_owner_project(project_slug)
    if competitor_domain not in (owner.get("competitors") or []):
        raise ValueError(
            f"'{competitor_domain}' no está registrado como competidor de '{project_slug}'. "
            "Los competidores se registran en projects.competitors (regla §4.3: nunca crawlear "
            "una URL arbitraria)."
        )

    start_url = f"https://{competitor_domain}"
    pages_limit = min(max_pages, MAX_PAGES_COMPETITOR) if max_pages else MAX_PAGES_COMPETITOR

    crawled_pages, errors = crawl_site(start_url, pages_limit)

    # Solo páginas con 200 real cuentan como "contenido" (regla P1): un 403/503
    # suele ser una página de challenge/WAF (ej. "Checking your browser...",
    # Cloudflare) — caso real detectado en myphonedoctor.co, cuyo title de
    # bloqueo se colaba como si fuera un tema de contenido del competidor.
    content_pages = [p for p in crawled_pages if p.status_code == 200]

    technical_scores = []
    sample_keywords: list[str] = []
    for page in content_pages:
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
        row = analyze_page(page_data).row
        green = sum(1 for v in row.values() if v == "green")
        technical_scores.append(green / len(row) if row else 0)

        # Proxy de "qué keywords targetea": title + H1, no es su ranking real
        # (P1: no fingimos tener datos de posicionamiento que no tenemos).
        if page.title:
            sample_keywords.append(page.title)
        sample_keywords.extend(page.h1_tags)

    technical_score = round(100 * sum(technical_scores) / len(technical_scores)) if technical_scores else None
    content_insights = _aggregate_content_insights(content_pages)

    geo_score = None
    site_reachable = False
    robots_confirmed = False
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            site_reachable = is_reachable(client, start_url)
            if site_reachable:
                llms_txt_exists, _ = _fetch_text(client, f"{start_url}/llms.txt")
                llms_full_exists, _ = _fetch_text(client, f"{start_url}/llms-full.txt")

                # No reusamos _fetch_text para robots.txt: necesitamos distinguir
                # "404 confirmado = sin restricciones" de "403/500/etc = no
                # sabemos si hay restricciones" (regla P1). _fetch_text colapsa
                # ambos casos en "" y asumir permisivo ahí sería un score falso
                # — caso real detectado con myphonedoctor.co (403 en robots.txt).
                robots_content = ""
                try:
                    robots_response = client.get(f"{start_url}/robots.txt")
                    if robots_response.status_code == 200:
                        robots_content = robots_response.text
                        robots_confirmed = True
                    elif robots_response.status_code == 404:
                        robots_confirmed = True  # confirmado: no existe, de verdad es permisivo
                except httpx.HTTPError:
                    robots_confirmed = False

                if robots_confirmed:
                    matrix = build_ai_crawler_matrix(robots_content)
                    geo_score, _breakdown = calculate_geo_score(llms_txt_exists, llms_full_exists, matrix)
    except Exception as exc:  # noqa: BLE001 - GEO del competidor es "nice to have", no crítico
        logger.warning("No se pudo calcular GEO score de %s: %s", competitor_domain, exc)

    # Honestidad de datos (regla P1): si el sitio no fue alcanzable, ni el
    # technical_score ni el geo_score deben presentarse como si fueran reales.
    unreachable = not crawled_pages and not site_reachable
    if unreachable:
        technical_score = None
        geo_score = None
        note = "Sitio inalcanzable o bloqueando nuestro crawler (robots.txt no legible → bloqueo conservador)"
    elif site_reachable and not robots_confirmed:
        note = "GEO Score no calculado: el sitio respondió pero bloqueó/falló la lectura de robots.txt (ej. 403), no podemos confirmar si tiene restricciones reales"
    else:
        note = None

    raw_data = {
        "competitor_domain": competitor_domain,
        "pages_crawled": len(crawled_pages),
        "technical_score": technical_score,
        "geo_score": geo_score,
        "sample_keywords": sorted(set(sample_keywords)),
        "errors": errors,
        "pages": [asdict(p) for p in crawled_pages],
        "reachable": site_reachable,
        "note": note,
        **content_insights,
    }

    now = now_iso()
    with get_connection() as conn:
        snapshot_id = conn.execute(
            insert(snapshots).values(
                project_id=owner["id"],
                collector=f"competitor:{competitor_domain}",
                status="ok" if crawled_pages else "error",
                started_at=now,
                finished_at=now_iso(),
                raw_data=raw_data,
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]

    return {
        "snapshot_id": snapshot_id,
        "competitor_domain": competitor_domain,
        "pages_crawled": len(crawled_pages),
        "technical_score": technical_score,
        "geo_score": geo_score,
        "sample_keywords_count": len(set(sample_keywords)),
        "reachable": site_reachable,
        "note": raw_data["note"],
        **content_insights,
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Escanea un competidor registrado (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True, help="slug del proyecto dueño")
    parser.add_argument("--competitor", required=True, help="dominio del competidor, ej: capriservicios.com")
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    result = scan_competitor(args.site, args.competitor, max_pages=args.max_pages)
    print(json.dumps(result, indent=2, ensure_ascii=False))
