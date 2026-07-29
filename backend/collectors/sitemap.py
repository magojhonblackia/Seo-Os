"""Collector de sitemap.xml (§ herramientas nuevas 2026-07-23).

Por qué hace falta: el crawler descubre páginas siguiendo enlaces internos, así
que nunca ve una página huérfana ni sabe cuántas páginas tiene el sitio de
verdad. El sitemap es la lista que el propio sitio DECLARA — comparándola con lo
crawleado y con lo indexado (analyzers/coverage.py) salen a la luz huérfanas,
URLs muertas declaradas y páginas reales fuera del sitemap.

Verificado contra jcreparaciones.com (2026-07-23): `<urlset>` plano con 331
`<loc>`, servido como application/xml sin gzip. Aun así se soporta:
- sitemap index (`<sitemapindex>`) → se bajan los sub-sitemaps (tope acotado),
- sitemaps .gz → se descomprimen con gzip de la stdlib,
porque son formas perfectamente válidas y comunes del estándar.

Ético (P5): mismo User-Agent identificable y el mismo rate limit de 1 req/s del
crawler. Degradación con gracia (S3): sin sitemap accesible se devuelve
'skipped' con el motivo, nunca se inventa una lista.
"""
from __future__ import annotations

import gzip
import logging
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import MIN_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

MAX_SUB_SITEMAPS = 25  # tope de sub-sitemaps de un índice, para no crawlear sin fin
MAX_URLS = 5000  # tope defensivo de URLs a guardar


def _decode_body(response: httpx.Response, url: str) -> str:
    """Un sitemap .gz llega como bytes comprimidos: httpx solo descomprime el
    Content-Encoding del transporte, no un ARCHIVO .gz."""
    content = response.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content).decode("utf-8", errors="replace")
        except OSError:  # no era gzip real
            pass
    return response.text


def discover_sitemap_urls(client: httpx.Client, site_url: str) -> list[str]:
    """Busca la declaración `Sitemap:` en robots.txt (fuente autoritativa) y cae
    a /sitemap.xml si no hay ninguna."""
    declared: list[str] = []
    try:
        resp = client.get(urljoin(site_url, "/robots.txt"))
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    declared.append(line.split(":", 1)[1].strip())
    except httpx.HTTPError as exc:
        logger.info("robots.txt no accesible en %s (%s); se prueba /sitemap.xml", site_url, exc)
    return declared or [urljoin(site_url, "/sitemap.xml")]


def _parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Devuelve (urls_de_paginas, urls_de_sub_sitemaps). BeautifulSoup con el
    parser xml resuelve el namespace del estándar sin tener que declararlo."""
    soup = BeautifulSoup(xml_text, "xml")
    if soup.find("sitemapindex"):
        subs = [loc.get_text(strip=True) for loc in soup.find_all("loc") if loc.get_text(strip=True)]
        return [], subs
    urls = [loc.get_text(strip=True) for loc in soup.find_all("loc") if loc.get_text(strip=True)]
    return urls, []


def fetch_sitemap_urls(site_url: str) -> tuple[list[str], list[str], list[str]]:
    """Devuelve (urls, sitemaps_consultados, errores). Sigue índices un nivel."""
    urls: list[str] = []
    consulted: list[str] = []
    errors: list[str] = []

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        pending = discover_sitemap_urls(client, site_url)
        sub_budget = MAX_SUB_SITEMAPS

        while pending and len(urls) < MAX_URLS:
            sitemap_url = pending.pop(0)
            if sitemap_url in consulted:
                continue
            consulted.append(sitemap_url)
            try:
                resp = client.get(sitemap_url)
                if resp.status_code != 200:
                    errors.append(f"{sitemap_url}: HTTP {resp.status_code}")
                    continue
                page_urls, sub_sitemaps = _parse_sitemap(_decode_body(resp, sitemap_url))
            except httpx.HTTPError as exc:
                errors.append(f"{sitemap_url}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - XML corrupto no debe tumbar la auditoría (S3)
                errors.append(f"{sitemap_url}: XML inválido ({exc})")
                continue

            urls.extend(page_urls)
            for sub in sub_sitemaps:
                if sub_budget <= 0:
                    break
                sub_budget -= 1
                pending.append(sub)

            time.sleep(MIN_DELAY_SECONDS)

    # dedup preservando orden
    seen: set[str] = set()
    deduped = [u for u in urls if not (u in seen or seen.add(u))]
    return deduped[:MAX_URLS], consulted, errors


class SitemapCollector(BaseCollector):
    name = "sitemap"

    def collect(self) -> CollectorResult:
        urls, consulted, errors = fetch_sitemap_urls(self.project["url"])

        if not urls:
            return CollectorResult(
                status="skipped",
                raw_data={"urls": [], "sitemaps_consulted": consulted, "errors": errors},
                error_message=(
                    "No se encontró ningún sitemap accesible con URLs. "
                    + ("Errores: " + "; ".join(errors[:3]) if errors else "Se probó robots.txt y /sitemap.xml.")
                ),
            )

        return CollectorResult(
            status="partial" if errors else "ok",
            raw_data={"urls": urls, "sitemaps_consulted": consulted, "errors": errors},
            error_message="; ".join(errors[:3]) if errors else None,
        )


def run_sitemap_collector(project_slug: str) -> dict:
    collector = SitemapCollector(project_slug)
    snapshot_id = collector.run()

    from sqlalchemy import select

    from backend.db.database import get_connection
    from backend.db.schema import snapshots as snapshots_table

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message).where(
                snapshots_table.c.id == snapshot_id
            )
        ).first()

    raw = (row[0] or {}) if row else {}
    return {
        "snapshot_id": snapshot_id,
        "status": row[1] if row else "error",
        "summary": {
            "urls_found": len(raw.get("urls", [])),
            "sitemaps_consulted": raw.get("sitemaps_consulted", []),
            "message": row[2] if row else None,
        },
    }
