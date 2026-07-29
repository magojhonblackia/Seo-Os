"""Resolución de redirects por red, acotada (§ causa nº1 de falsos positivos).

Por qué existe: el mapa de redirects que sale del crawler solo cubre las URLs
que el crawler VISITÓ. Pero Search Console reporta URLs que quizá nunca
crawleamos (quedaron fuera del límite de páginas o no están enlazadas), y esas
son justo las que generan canibalizaciones falsas. Caso real verificado en
jcreparaciones.com:

    /reparacion-macbook-sevilla-valle  →308→  /reparacion-macbook/sevilla-valle

Se reportaba como "2 páginas compitiendo" cuando es UNA página con una entrada
vieja.

Vive en collectors/ (no en analyzers/) a propósito: los analyzers son funciones
puras y testeables sin red — este módulo hace la I/O y les entrega el mapa ya
resuelto.

Ético (P5): mismo User-Agent identificable, 1 req/s, y un TOPE de peticiones
para no convertir un análisis en un crawl encubierto. Se usa HEAD (sin cuerpo).
"""
from __future__ import annotations

import logging
import time

import httpx

from backend.analyzers.coverage import canonical_url
from backend.collectors.crawler import MIN_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

MAX_REDIRECT_LOOKUPS = 40  # tope defensivo: solo se resuelven candidatos, no el sitio entero


def resolve_redirect_targets(
    urls: list[str],
    known_map: dict[str, str] | None = None,
    max_requests: int = MAX_REDIRECT_LOOKUPS,
) -> dict[str, str]:
    """Devuelve un mapa canónico origen→destino SOLO para las URLs que de verdad
    redirigen. Omite las que ya están en `known_map` (las que el crawler ya vio).

    Nunca lanza: un fallo de red deja esa URL sin resolver (se tratará como
    página propia), que es el comportamiento conservador correcto — preferimos
    no resolver a inventar un destino.
    """
    known = dict(known_map or {})
    pending: list[str] = []
    seen: set[str] = set()
    for url in urls:
        c = canonical_url(url)
        if not c or c in known or c in seen:
            continue
        seen.add(c)
        pending.append(url)

    resolved: dict[str, str] = {}
    if not pending:
        return resolved

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for url in pending[:max_requests]:
            try:
                response = client.head(url)
                # Algunos servidores no soportan HEAD; se reintenta con GET solo
                # en ese caso concreto (405/501), no en cualquier error.
                if response.status_code in (405, 501):
                    response = client.get(url)
                if response.history:
                    src, dest = canonical_url(url), canonical_url(str(response.url))
                    if src and dest and src != dest:
                        resolved[src] = dest
            except httpx.HTTPError as exc:
                logger.info("No se pudo resolver redirect de %s: %s", url, exc)
            time.sleep(MIN_DELAY_SECONDS)

    if len(pending) > max_requests:
        logger.info(
            "Resolución de redirects acotada a %s de %s URLs candidatas", max_requests, len(pending)
        )
    return resolved
