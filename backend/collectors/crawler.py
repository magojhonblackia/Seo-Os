"""Crawler técnico propio (regla P5 del PROMPT_MAESTRO: ético y responsable).

- Máx 1 request/segundo por dominio.
- Respeta robots.txt (urllib.robotparser).
- User-Agent identificable, timeout 15s, máx 5 redirects.
- Máx 500 páginas del propio sitio, 50 de un competidor (regla §4.3).
- Solo sigue enlaces internos (mismo dominio registrado).
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import insert, select, update

from backend.analyzers.business_rules import build_banned_phrase_issues
from backend.analyzers.content import (
    EeatSignals,
    build_answer_extractability_issues,
    build_content_issues,
    build_fact_density_issues,
    build_freshness_issues,
    calculate_eeat_score,
    flesch_huerta_score,
)
from backend.analyzers.issue_store import reconcile_page_issues, record_issue
from backend.analyzers.technical import PageData, analyze_page, is_noindex_justified
from backend.collectors import progress
from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import configure_logging
from backend.db.database import get_connection, now_iso
from backend.db.schema import pages

logger = logging.getLogger(__name__)

USER_AGENT = "SEO-OS-Bot/1.0 (+auditoria interna; sin fines comerciales)"
REQUEST_TIMEOUT = 15.0
MIN_DELAY_SECONDS = 1.0
MAX_PAGES_OWN_SITE = 500
MAX_PAGES_COMPETITOR = 50
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2MB, ignorar binarios grandes


@dataclass
class CrawledPage:
    url: str
    status_code: int | None = None
    title: str | None = None
    meta_description: str | None = None
    h1_tags: list[str] = field(default_factory=list)
    schema_types: list[str] = field(default_factory=list)
    schema_has_errors: bool = False
    og: dict = field(default_factory=dict)
    canonical: str | None = None
    is_indexable: bool = True
    lang_declared: str = "es"
    word_count: int = 0
    internal_links: list[str] = field(default_factory=list)
    nofollow_internal: list[str] = field(default_factory=list)  # desperdicia autoridad (§ #6)
    nofollow_external_count: int = 0  # informativo: en externos es uso correcto, NO se reporta
    fetch_error: str | None = None
    fetched_at: str | None = None  # ISO 8601 del momento exacto del fetch (§ frescura: auditar snapshot viejo)
    redirected_to: str | None = None  # URL final si la enlazada redirigía (§ cobertura: enlazar directo al destino)
    x_robots_tag: str | None = None  # header HTTP crudo (§ X-Robots-Tag: puede contradecir el meta robots del HTML)
    content_encoding: str | None = None  # gzip/br/deflate — ausente = sin comprimir (§ cache/compression)
    cache_control: str | None = None  # ausente = el navegador aplica heurística RFC 7234, el mismo bug que tuvimos
    http_last_modified: str | None = None  # § mejoras 2026-08-02: cruzar contra dateModified del schema
    schema_date_published: str | None = None
    schema_date_modified: str | None = None
    body_text_sample: str = ""  # primeros ~5000 chars, para legibilidad (§9 Fase 1)
    has_author: bool = False
    has_date: bool = False
    has_contact: bool = False
    local_business_schema: dict | None = None  # name/telephone del JSON-LD LocalBusiness (§9 Fase 4)
    schema_nodes: list[dict] = field(default_factory=list)  # {types, fields} para validar Rich Results (§ #5)
    answer_extractability: dict = field(default_factory=dict)  # § mejoras 2026-08-02, ver _analyze_answer_extractability
    fact_density: dict = field(default_factory=dict)  # § mejoras 2026-08-02, ver _analyze_fact_density
    banned_phrases_found: list[str] = field(default_factory=list)  # § mejoras 2026-08-26, ver README


def _same_domain(url: str, root_netloc: str) -> bool:
    return urlparse(url).netloc.replace("www.", "") == root_netloc.replace("www.", "")


def _normalize_url(url: str) -> str:
    """Evita duplicados triviales: quita slash final (salvo raíz) y fragmento."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(path=path, fragment="").geturl()


def _extract_schema_types(soup: BeautifulSoup) -> tuple[list[str], bool]:
    types: list[str] = []
    has_errors = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            has_errors = True
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            nodes = graph if isinstance(graph, list) else [candidate]
            for node in nodes:
                if isinstance(node, dict) and "@type" in node:
                    t = node["@type"]
                    types.extend(t if isinstance(t, list) else [t])
    return types, has_errors


def _schema_field_paths(node: dict) -> list[str]:
    """Claves del nodo + un nivel de anidamiento en los contenedores conocidos
    (offers.price, aggregateRating.ratingValue…). Se guardan solo los NOMBRES de
    campo, no los valores: basta para validar campos requeridos y mantiene el
    snapshot pequeño."""
    nested_containers = {"offers", "aggregateRating", "address", "geo", "author", "publisher"}
    paths: list[str] = []
    for key, value in node.items():
        if key.startswith("@"):
            continue
        paths.append(key)
        if key in nested_containers:
            child = value[0] if isinstance(value, list) and value else value
            if isinstance(child, dict):
                paths.extend(f"{key}.{k}" for k in child if not k.startswith("@"))
    return sorted(set(paths))


def _extract_schema_nodes(soup: BeautifulSoup) -> list[dict]:
    """Nodos JSON-LD resumidos como {types, fields} para validar campos
    requeridos de Rich Results (§ #5: hasta ahora solo mirábamos el @type, así
    que un LocalBusiness sin address se reportaba como 'schema OK' aunque Google
    no fuera a mostrar el rich snippet)."""
    nodes: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            raw_nodes = graph if isinstance(graph, list) else [candidate]
            for node in raw_nodes:
                if not isinstance(node, dict) or "@type" not in node:
                    continue
                types = node["@type"]
                nodes.append(
                    {
                        "types": [t for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)],
                        "fields": _schema_field_paths(node),
                        # § mejoras 2026-08-28: permite mergear por @id en
                        # schema_validation.py — un LocalBusiness puede venir
                        # partido en 2 <script> distintos que comparten @id
                        # (layout.tsx declara el nodo completo, page.tsx solo
                        # agrega un campo más), patrón válido de JSON-LD.
                        "id": node.get("@id") if isinstance(node.get("@id"), str) else None,
                    }
                )
    return nodes


def _extract_schema_dates(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """(datePublished, dateModified) tal cual vienen en el JSON-LD.

    § mejoras 2026-08-02: EXCEPCIÓN deliberada a la regla de
    `_extract_schema_nodes` (solo nombres de campo, nunca valores, ver su
    docstring) — para detectar frescura hace falta el VALOR real de la
    fecha, para cruzarlo contra el header HTTP Last-Modified real del
    servidor. Sin el valor no hay nada que cruzar. Nunca se usa para
    escribir/backdatear nada, solo para comparar (P1)."""
    published: str | None = None
    modified: str | None = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            raw_nodes = graph if isinstance(graph, list) else [candidate]
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                if published is None and isinstance(node.get("datePublished"), str):
                    published = node["datePublished"]
                if modified is None and isinstance(node.get("dateModified"), str):
                    modified = node["dateModified"]
    return published, modified


def _extract_local_business_schema(soup: BeautifulSoup) -> dict | None:
    """Busca un nodo JSON-LD con @type LocalBusiness y devuelve su name/telephone
    (§9 Fase 4, análisis NAP). Coincidencia estricta con "LocalBusiness" — el
    mismo tipo que usa el generador de schema con IA — subtipos como
    "Restaurant" no se detectan aquí, es una limitación conocida y documentada."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            nodes = graph if isinstance(graph, list) else [candidate]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_types = node.get("@type")
                node_types = node_types if isinstance(node_types, list) else [node_types]
                if "LocalBusiness" in node_types:
                    return {"name": node.get("name"), "telephone": node.get("telephone")}
    return None


_WHITESPACE_RE = re.compile(r"\s+")


def _extract_h1_text(h1) -> str:
    """Texto de un H1 tratando <br> como el salto de línea que es (=> espacio),
    pero manteniendo los spans/tags INLINE pegados (no parte palabras).

    Un <br/> en el fuente es una división visual legítima, no un bug: al
    aplanar el texto debe convertirse en espacio. El bug real de "palabras
    pegadas" es una concatenación SIN ningún tag ni espacio de por medio
    (ej. "ReparacióniPhone" en un solo nodo de texto) — eso sí lo sigue
    detectando technical.detect_stuck_words sobre este texto ya normalizado.
    Antes se extraía con get_text(strip=True) sin separador y "REPARACIÓN DE
    <br/> CELULARES" quedaba como "REPARACIÓN DECELULARES" (texto basura que
    además ensuciaba word_count, el chequeo de keyword y el contexto para la IA).
    """
    node = copy.copy(h1)
    for br in node.find_all("br"):
        br.replace_with(" ")
    return _WHITESPACE_RE.sub(" ", node.get_text()).strip()


def _extract_page_data(url: str, status_code: int, html: str, banned_phrases: list[str] | None = None) -> CrawledPage:
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag.get("content", "").strip() if meta_desc_tag else None

    h1_tags = [t for t in (_extract_h1_text(h1) for h1 in soup.find_all("h1")) if t]

    schema_types, schema_has_errors = _extract_schema_types(soup)

    og = {}
    for prop in ("title", "description", "image"):
        tag = soup.find("meta", property=f"og:{prop}")
        if tag and tag.get("content"):
            og[prop] = tag["content"]

    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href") if canonical_tag else None

    robots_tag = soup.find("meta", attrs={"name": "robots"})
    robots_content = (robots_tag.get("content", "") if robots_tag else "").lower()
    is_indexable = "noindex" not in robots_content

    html_tag = soup.find("html")
    lang_declared = (html_tag.get("lang", "es") if html_tag else "es").split("-")[0] or "es"

    body_text = soup.get_text(separator=" ", strip=True)
    word_count = len(body_text.split())

    internal_links = []
    nofollow_internal: list[str] = []
    nofollow_external_count = 0
    root_netloc = urlparse(url).netloc
    for a in soup.find_all("a", href=True):
        absolute = urljoin(url, a["href"])
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        # rel puede venir como lista (bs4 lo parsea así) o string.
        rel_raw = a.get("rel") or []
        rel = {r.lower() for r in (rel_raw if isinstance(rel_raw, list) else str(rel_raw).split())}
        is_internal = _same_domain(absolute, root_netloc)
        if is_internal:
            internal_links.append(absolute.split("#")[0])
            # Solo el nofollow INTERNO es un problema (desperdicia autoridad
            # dentro del propio sitio). En enlaces externos —redes sociales,
            # WhatsApp— es uso correcto y NO debe reportarse: distinguirlos
            # evita generar verificación manual en cada reporte (§ #6).
            if "nofollow" in rel:
                nofollow_internal.append(absolute.split("#")[0])
        elif "nofollow" in rel:
            nofollow_external_count += 1

    has_author, has_date, has_contact = _detect_eeat_signals(soup, body_text, schema_types)
    local_business_schema = _extract_local_business_schema(soup)
    schema_nodes = _extract_schema_nodes(soup)
    answer_extractability = _analyze_answer_extractability(soup)
    fact_density = _analyze_fact_density(soup, url)
    schema_date_published, schema_date_modified = _extract_schema_dates(soup)

    # § mejoras 2026-08-26 (auditoría manual contra jcreparaciones.com,
    # categoría 3 del prompt): reglas de negocio prohibidas SON específicas de
    # cada proyecto (ej. "diagnóstico gratis" en JC), así que viven en
    # project.config["banned_phrases"], no hardcodeadas aquí. Se busca sobre
    # el texto COMPLETO de la página (body_text, antes de recortar a los 5000
    # caracteres de body_text_sample) — una frase prohibida puede estar en
    # cualquier parte de la página, no solo al principio.
    banned_phrases_found = sorted(
        {phrase for phrase in (banned_phrases or []) if phrase and phrase.lower() in body_text.lower()}
    )

    return CrawledPage(
        url=url,
        status_code=status_code,
        title=title,
        meta_description=meta_description,
        h1_tags=h1_tags,
        schema_types=schema_types,
        schema_has_errors=schema_has_errors,
        og=og,
        canonical=canonical,
        is_indexable=is_indexable,
        lang_declared=lang_declared,
        word_count=word_count,
        internal_links=internal_links,
        nofollow_internal=nofollow_internal,
        nofollow_external_count=nofollow_external_count,
        fetched_at=now_iso(),
        body_text_sample=body_text[:5000],
        has_author=has_author,
        has_date=has_date,
        has_contact=has_contact,
        local_business_schema=local_business_schema,
        schema_nodes=schema_nodes,
        answer_extractability=answer_extractability,
        fact_density=fact_density,
        schema_date_published=schema_date_published,
        schema_date_modified=schema_date_modified,
        banned_phrases_found=banned_phrases_found,
    )


_AUTHOR_TEXT_RE = re.compile(r"\b(por|escrito por|autor|redactado por)\s*:?\s*\w+", re.IGNORECASE)
_DATE_TEXT_RE = re.compile(
    r"\b(actualizado|publicado|última actualización)\b.{0,30}(\d{4}|\d{1,2}\s+de\s+\w+)"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(\+?57[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_CONTACT_WORDS_RE = re.compile(r"\b(contáctanos|contacto|whatsapp|escríbenos)\b", re.IGNORECASE)


def _detect_eeat_signals(soup: BeautifulSoup, body_text: str, schema_types: list[str]) -> tuple[bool, bool, bool]:
    """Heurísticas de señales E-E-A-T (§9 Fase 1) — no son un detector perfecto,
    son señales razonables: byline, fecha visible, información de contacto."""
    has_author = (
        bool(soup.find("meta", attrs={"name": "author"}))
        or "Person" in schema_types
        or bool(_AUTHOR_TEXT_RE.search(body_text[:1000]))
    )

    has_date = (
        bool(soup.find("meta", property="article:published_time"))
        or bool(soup.find("meta", property="article:modified_time"))
        or bool(soup.find("time"))
        or bool(_DATE_TEXT_RE.search(body_text))
    )

    has_contact = (
        bool(soup.find("a", href=re.compile(r"^mailto:", re.IGNORECASE)))
        or bool(_PHONE_RE.search(body_text))
        or bool(_CONTACT_WORDS_RE.search(body_text))
    )

    return has_author, has_date, has_contact


# § mejoras 2026-08-02: extraíbilidad de respuestas por IA, adaptado del
# módulo M11 del plugin claude-seo-ai (revisado en detalle antes de portar
# nada — ver README). El original está en inglés (regex sobre "how/what/
# why..."); estos patrones son una adaptación propia al español, sin
# verificación previa contra datos reales en inglés, así que se probó en
# vivo contra jcreparaciones.com antes de confiar en ella (ver README).
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# "Cómo cambiar/hacer/reparar..." (infinitivo) = título de guía instructiva,
# no una pregunta que necesite una respuesta corta — mismo criterio que el
# original excluye "How to X" en inglés.
_HOWTO_TITLE_RE = re.compile(r"^¿?\s*c[oó]mo\s+\w+(ar|er|ir)\b", re.IGNORECASE)
_QUESTION_START_RE = re.compile(
    r"^¿?\s*(qué|que|cómo|como|por\s+qué|por\s+que|cuándo|cuando|dónde|donde|"
    r"quién|quien|cuáles?|cuales?|cuánto[as]?|cuanto[as]?|puedo|puedes|puede|"
    r"debo|debes|debe|es\s|son\s|hay\s)",
    re.IGNORECASE,
)
# Frase de relleno antes de la respuesta real ("en este artículo veremos...")
# — cuenta como "sin respuesta directa" aunque el párrafo no esté vacío.
_WINDUP_RE = re.compile(
    r"^(en (este|esta) (sección|artículo|guía|post|apartado)|vamos a (ver|explorar|hablar|revisar)|"
    r"a continuación|te explicamos|en este blog|primero,)",
    re.IGNORECASE,
)
_TLDR_RE = re.compile(r"\b(resumen|en resumen|en pocas palabras|puntos clave|lo esencial|tl;?dr)\b", re.IGNORECASE)

# § mejoras 2026-08-26 (auditoría manual contra jcreparaciones.com, ver README):
# un encabezado-pregunta con un párrafo NO vacío y sin frase de relleno pasaba
# como "con respuesta" aunque el párrafo fuera puro CTA de marketing
# ("cotización", "escríbenos") sin ningún dato concreto — caso real encontrado
# en /por-que-no-carga-mi-celular-cali: el H1 pregunta "¿por qué no carga?" y
# el párrafo siguiente solo repite síntomas + empuja WhatsApp, nunca nombra
# una causa. Un CTA no es lo mismo que una respuesta, así que se cuenta aparte
# de `without_answer` (el fix es distinto: no es quitar el CTA, es agregar el
# dato que falta al lado).
_CTA_ONLY_RE = re.compile(
    r"\b(cotizaci[oó]n|cotiza(r|mos)?|consultar\s+ahora|escr[ií]benos|cont[aá]ctanos|contacta(nos)?|"
    r"whatsapp|agenda\s+tu\s+cita|ll[aá]manos|ven\s+(sin\s+cita|a\s+vernos)|reserva\s+tu\s+cita)\b",
    re.IGNORECASE,
)


def _looks_like_question_heading(text: str) -> bool:
    """Bug real encontrado verificando en vivo contra jc: "¿Cómo saber si la
    batería está degradada?" es una pregunta real (termina en "?") pero
    "saber" también es infinitivo, así que la exclusión de título-guía la
    descartaba antes de mirar el signo de pregunta. El "?" final es la señal
    más fuerte — debe ganarle a la heurística de título-guía, no al revés."""
    text = text.strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    if _HOWTO_TITLE_RE.match(text):
        return False
    return bool(_QUESTION_START_RE.match(text))


def _first_paragraph_after(heading_tag) -> str:
    """Primer <p>/<li> con texto real después de este heading, antes del
    siguiente heading — es lo que un motor de IA leería como "la respuesta"."""
    for sib in heading_tag.find_all_next():
        if sib.name in _HEADING_TAGS:
            break
        if sib.name in ("p", "li"):
            text = sib.get_text(separator=" ", strip=True)
            if text:
                return text
    return ""


def _analyze_answer_extractability(soup: BeautifulSoup) -> dict:
    """Cuenta encabezados en forma de pregunta con/sin una respuesta directa
    corta justo después (banda ideal 40-60 palabras, igual que el original) —
    eso es lo que un motor de IA puede citar sin tener que leer todo el
    artículo. Heurística determinística sobre texto, no usa IA para juzgar."""
    headings = [h for h in soup.find_all(_HEADING_TAGS) if h.get_text(strip=True)]

    question_count = 0
    without_answer = 0
    in_target_band = 0
    cta_only = 0
    for h in headings:
        if not _looks_like_question_heading(h.get_text(separator=" ", strip=True)):
            continue
        question_count += 1
        answer = _first_paragraph_after(h)
        word_count = len(answer.split())
        has_direct_answer = word_count > 0 and not _WINDUP_RE.match(answer)
        if not has_direct_answer:
            without_answer += 1
        elif _CTA_ONLY_RE.search(answer) and not _NUMERIC_RE.search(answer):
            cta_only += 1
        if 40 <= word_count <= 60:
            in_target_band += 1

    paragraphs = [
        p.get_text(separator=" ", strip=True) for p in soup.find_all(("p", "li"))
    ]
    long_passages = sum(1 for p in paragraphs if len(p.split()) > 250)

    body_sample = soup.get_text(separator=" ", strip=True)[:4000]
    has_tldr = bool(_TLDR_RE.search(body_sample)) or any(_TLDR_RE.search(h.get_text()) for h in headings)

    return {
        "question_headings_count": question_count,
        "question_headings_without_answer": without_answer,
        "question_headings_answered_with_cta_only": cta_only,
        "answers_in_target_band": in_target_band,
        "long_passages_over_250w": long_passages,
        "has_tldr_or_summary": has_tldr,
    }


# § mejoras 2026-08-02: densidad de datos/hechos, adaptado del módulo M12 de
# claude-seo-ai (mismo criterio que M11: adaptación propia al español,
# verificada en vivo contra jcreparaciones.com antes de confiar en ella).
_NUMERIC_RE = re.compile(
    r"\b\d+([.,]\d+)?\s?(%|por\s?ciento|x|mil|millones?)?\b|\$\s?\d|\b(19|20)\d{2}\b",
    re.IGNORECASE,
)
_ORIGINAL_DATA_RE = re.compile(
    r"\b(nuestr[oa]s?\s+(estudio|encuesta|datos|investigaci[oó]n|an[aá]lisis|prueba|experimento)|"
    r"analizamos|encuestamos|probamos|medimos|encontramos que|datos (propios|internos)|"
    r"en nuestra (prueba|encuesta))\b",
    re.IGNORECASE,
)
_SUPERLATIVE_RE = re.compile(
    r"\b(el\s+(mejor|m[aá]s\s+r[aá]pido|m[aá]s\s+grande|n[uú]mero\s+uno|l[ií]der)|#1|"
    r"m[aá]s\s+barato|(los\s+)?estudios\s+(demuestran|muestran)|"
    r"expertos\s+(coinciden|dicen|aseguran)|todo\s+el\s+mundo\s+sabe)\b",
    re.IGNORECASE,
)
_AUTHORITY_HOST_RE = re.compile(
    r"(\.gov(\.\w+)?|\.edu(\.\w+)?|\.gob\.\w+|wikipedia\.org|nature\.com|sciencedirect|"
    r"nih\.gov|who\.int|oecd\.org|arxiv\.org|w3\.org|schema\.org)",
    re.IGNORECASE,
)


def _analyze_fact_density(soup: BeautifulSoup, page_url: str) -> dict:
    """Cuenta párrafos largos (≥30 palabras) sin ningún número/estadística
    (afirmaciones que suenan vagas), frases de autoridad vaga sin fuente
    ("los expertos dicen"), señal de datos propios/originales, y enlaces
    salientes a fuentes de autoridad (.gov/.edu/wikipedia). Solo cuenta —
    juzgar si una afirmación "necesita" fuente es criterio humano, nunca se
    inventa una estadística o fuente para "mejorar" el conteo."""
    paragraphs = [
        p.get_text(separator=" ", strip=True) for p in soup.find_all(("p", "li"))
    ]
    paragraphs = [p for p in paragraphs if len(p.split()) >= 8]
    text = " ".join(paragraphs)
    total_words = len(text.split())
    numeric_tokens = len(_NUMERIC_RE.findall(text))

    passages_without_numbers = sum(
        1 for p in paragraphs if len(p.split()) >= 30 and not _NUMERIC_RE.search(p)
    )

    root_netloc = urlparse(page_url).netloc
    authority_links = 0
    for a in soup.find_all("a", href=True):
        absolute = urljoin(page_url, a["href"])
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or _same_domain(absolute, root_netloc):
            continue
        if _AUTHORITY_HOST_RE.search(parsed.netloc):
            authority_links += 1

    return {
        "numeric_density_per_100w": round(100 * numeric_tokens / total_words, 2) if total_words else 0.0,
        "passages_30w_plus_without_numbers": passages_without_numbers,
        "original_data_signal": bool(_ORIGINAL_DATA_RE.search(text)),
        "vague_authority_claims_count": len(_SUPERLATIVE_RE.findall(text)),
        "authoritative_outbound_links": authority_links,
    }


def _load_robots(start_url: str) -> RobotFileParser:
    """Si robots.txt no se puede leer (timeout, DNS, conexión rechazada),
    RobotFileParser.can_fetch() de la librería estándar devuelve False para
    TODO (nunca fijó `last_checked`) — es decir, se bloquea por defecto, no se
    permite. Postura conservadora correcta (regla P5): ante la duda, no crawlear.
    """
    parser = RobotFileParser()
    parser.set_url(urljoin(start_url, "/robots.txt"))
    try:
        parser.read()
    except Exception:  # noqa: BLE001
        logger.warning(
            "No se pudo leer robots.txt de %s (sitio inalcanzable o bloqueando); "
            "se bloqueará el crawl por defecto (comportamiento conservador de robots.txt)",
            start_url,
        )
    return parser


def crawl_site(
    start_url_raw: str,
    max_pages: int,
    on_progress=None,
    banned_phrases: list[str] | None = None,
) -> tuple[list[CrawledPage], list[str]]:
    """Bucle de rastreo compartido (regla del Arquitecto: no duplicar lógica).

    Usado tanto por TechnicalCrawler (sitio propio) como por CompetitorCollector
    (sitio de un competidor registrado) — mismas reglas éticas (P5): rate limit,
    robots.txt, User-Agent identificable, límite de páginas.

    on_progress: callback opcional (done, queued, current_url) llamado en cada
    página, para reportar avance en vivo a la barra de progreso (UX, no altera
    ningún dato). Nunca debe lanzar — el crawl no depende de él.
    """
    start_url = _normalize_url(start_url_raw)
    root_netloc = urlparse(start_url).netloc
    robots = _load_robots(start_url)

    visited: set[str] = set()
    queue: deque[str] = deque([start_url])
    crawled_pages: list[CrawledPage] = []
    errors: list[str] = []

    with httpx.Client(
        # Cache-Control/Pragma no-cache: pedimos a proxies/CDN intermedios el
        # HTML más fresco posible, para no analizar una copia vieja (§ frescura).
        # Nota honesta: NO garantiza saltar el edge cache de ISR de Vercel
        # (eso lo decide el origen, no el cliente) — es la palanca correcta del
        # lado del cliente, pero el timestamp `fetched_at` por página es lo que
        # permite auditar si un issue vino de HTML viejo.
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
    ) as client:
        while queue and len(crawled_pages) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            if not robots.can_fetch(USER_AGENT, url):
                logger.info("robots.txt bloquea %s, se omite", url)
                continue

            try:
                response = client.get(url)
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    continue
                if len(response.content) > MAX_BODY_BYTES:
                    continue
                page = _extract_page_data(url, response.status_code, response.text, banned_phrases=banned_phrases)
                # response.history no está vacío cuando httpx siguió una o más
                # redirecciones: la URL enlazada internamente NO es la final.
                # Enlazar directo al destino ahorra un salto (§ cobertura).
                if response.history:
                    page.redirected_to = str(response.url)
                page.x_robots_tag = response.headers.get("x-robots-tag")
                page.content_encoding = response.headers.get("content-encoding")
                page.cache_control = response.headers.get("cache-control")
                page.http_last_modified = response.headers.get("last-modified")
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                logger.warning("Error crawleando %s: %s", url, exc)
                time.sleep(MIN_DELAY_SECONDS)
                continue

            crawled_pages.append(page)
            for raw_link in page.internal_links:
                link = _normalize_url(raw_link)
                if link not in visited and _same_domain(link, root_netloc):
                    queue.append(link)

            if on_progress is not None:
                try:
                    on_progress(len(crawled_pages), len(queue), url)
                except Exception:  # noqa: BLE001 - el progreso es solo UX, nunca rompe el crawl
                    pass

            time.sleep(MIN_DELAY_SECONDS)

    return crawled_pages, errors


class TechnicalCrawler(BaseCollector):
    name = "crawler"

    def __init__(self, project_slug: str, max_pages: int | None = None, is_competitor: bool = False):
        super().__init__(project_slug)
        default_max = MAX_PAGES_COMPETITOR if is_competitor else MAX_PAGES_OWN_SITE
        self.max_pages = min(max_pages, default_max) if max_pages else default_max
        self.is_competitor = is_competitor

    def collect(self) -> CollectorResult:
        slug = self.project["slug"]
        progress.start(slug, self.max_pages, phase="crawling")
        # Las reglas de negocio prohibidas son del PROYECTO auditado, nunca
        # del competidor (is_competitor) — no tendría sentido exigirle a un
        # competidor que cumpla las reglas de negocio de otra empresa.
        banned_phrases = (self.project.get("config") or {}).get("banned_phrases") or [] if not self.is_competitor else []
        crawled_pages, errors = crawl_site(
            self.project["url"],
            self.max_pages,
            on_progress=lambda done, queued, url: progress.update(slug, done, url),
            banned_phrases=banned_phrases,
        )
        progress.set_phase(slug, "analyzing")

        raw_data = {"pages": [asdict(p) for p in crawled_pages], "errors": errors}
        status = "ok" if crawled_pages and not errors else ("partial" if crawled_pages else "error")
        return CollectorResult(
            status=status,
            raw_data=raw_data,
            error_message="; ".join(errors[:5]) if errors else None,
        )

    def persist_analysis(self, snapshot_id: int, raw_pages: list[dict]) -> dict:
        """Analiza cada página crawleada (Formato Mago) y persiste pages + issues.

        Devuelve un resumen para mostrar de inmediato en el reporte de la corrida.
        """
        summary = {
            "pages_analyzed": 0,
            "issues_created": 0,
            "issues_resolved": 0,  # issues viejas cerradas porque ya no se reproducen (reconciliación)
            "by_severity": {"critical": 0, "high": 0, "medium": 0},
        }
        expected_lang = self.project.get("language", "es")
        # § mejoras 2026-08-28: ver docstring de is_noindex_justified()
        # (backend/analyzers/technical.py) — bug real corregido ahí, función
        # compartida con page_data_from_stored_row/calculate_technical_score/
        # _rebuild_technical_report para que el semáforo sea consistente en
        # los 4 lugares que reconstruyen PageData, no solo aquí.
        noindex_justified_patterns = (self.project.get("config") or {}).get("noindex_justified_patterns") or []

        with get_connection() as conn:
            for raw in raw_pages:
                # § mejoras 2026-08-28 (hallazgo real de una auditoría manual
                # contra jcreparaciones.com): una URL 404 real (link interno
                # muerto, entrada vieja de sitemap/índice de Google) devuelve
                # 200-de-facto en el sentido de que httpx SÍ trae HTML — el
                # de la página de error compartida de Next.js. Sin este gate,
                # esa plantilla se analizaba como si fuera contenido real de
                # CADA URL rota, generando issues de "meta description corta",
                # "legibilidad baja", "doble sufijo de marca", etc. — todas
                # describiendo el MISMO template genérico repetido, no un
                # problema real de contenido. La URL rota en sí YA se reporta
                # una sola vez, agregada, vía find_broken_pages/site_health —
                # no hace falta (ni es honesto) además analizarle el "contenido".
                is_broken = isinstance(raw.get("status_code"), int) and raw["status_code"] >= 400

                if is_broken:
                    report_issues: list = []
                    content_issues: list = []
                    readability_score = None
                    eeat_score = None
                    eeat_signals = EeatSignals(has_author=False, has_date=False, is_https=raw["url"].startswith("https://"), has_contact=False, word_count=0)
                    answer_extractability: dict = {}
                    fact_density: dict = {}
                else:
                    page_data = PageData(
                        url=raw["url"],
                        title=raw.get("title"),
                        meta_description=raw.get("meta_description"),
                        h1_tags=raw.get("h1_tags", []),
                        schema_types=raw.get("schema_types", []),
                        schema_has_errors=raw.get("schema_has_errors", False),
                        og=raw.get("og", {}),
                        canonical=raw.get("canonical"),
                        is_indexable=raw.get("is_indexable", True),
                        noindex_justified=is_noindex_justified(raw["url"], noindex_justified_patterns),
                        lang_declared=expected_lang,
                        x_robots_tag=raw.get("x_robots_tag"),
                    )
                    report_issues = analyze_page(page_data).issues

                    readability_score = flesch_huerta_score(raw.get("body_text_sample", ""))
                    eeat_signals = EeatSignals(
                        has_author=raw.get("has_author", False),
                        has_date=raw.get("has_date", False),
                        is_https=raw["url"].startswith("https://"),
                        has_contact=raw.get("has_contact", False),
                        word_count=raw.get("word_count", 0),
                    )
                    eeat_score, _eeat_breakdown = calculate_eeat_score(eeat_signals)
                    content_issues = build_content_issues(raw["url"], readability_score, eeat_score, eeat_signals)
                    answer_extractability = raw.get("answer_extractability") or {}
                    content_issues += build_answer_extractability_issues(raw["url"], answer_extractability)
                    fact_density = raw.get("fact_density") or {}
                    content_issues += build_fact_density_issues(raw["url"], fact_density)
                    content_issues += build_freshness_issues(
                        raw["url"], raw.get("schema_date_modified"), raw.get("http_last_modified")
                    )
                    content_issues += build_banned_phrase_issues(raw["url"], raw.get("banned_phrases_found") or [])

                existing = conn.execute(
                    select(pages.c.id).where(
                        pages.c.project_id == self.project["id"], pages.c.url == raw["url"]
                    )
                ).first()
                now = now_iso()
                # Para páginas rotas, no se guarda título/meta/H1/word_count:
                # ese texto es el de la plantilla de error compartida, no
                # contenido real de esta URL — guardarlo como si lo fuera
                # contaminaría promedios y agregados del reporte (ver comentario
                # arriba de is_broken).
                page_values = {
                    "project_id": self.project["id"],
                    "url": raw["url"],
                    "last_crawled": now,
                    "status_code": raw.get("status_code"),
                    "title": None if is_broken else raw.get("title"),
                    "meta_description": None if is_broken else raw.get("meta_description"),
                    "h1": None if is_broken else (" | ".join(raw.get("h1_tags", [])) or None),
                    "canonical": raw.get("canonical"),
                    "robots_meta": None,
                    "schema_types": [] if is_broken else raw.get("schema_types", []),
                    "og": {} if is_broken else raw.get("og", {}),
                    "word_count": None if is_broken else raw.get("word_count"),
                    "lang_detected": raw.get("lang_declared"),
                    "is_indexable": raw.get("is_indexable", True),
                    "x_robots_tag": raw.get("x_robots_tag"),
                    "redirected_to": raw.get("redirected_to"),
                    "readability_score": None if readability_score is None else round(readability_score),
                    "eeat_score": eeat_score,
                    "has_author": eeat_signals.has_author,
                    "has_date": eeat_signals.has_date,
                    "has_contact": eeat_signals.has_contact,
                    "question_headings_count": answer_extractability.get("question_headings_count"),
                    "question_headings_without_answer": answer_extractability.get("question_headings_without_answer"),
                    "question_headings_answered_with_cta_only": answer_extractability.get("question_headings_answered_with_cta_only"),
                    "answers_in_target_band": answer_extractability.get("answers_in_target_band"),
                    "long_passages_over_250w": answer_extractability.get("long_passages_over_250w"),
                    "has_tldr_or_summary": answer_extractability.get("has_tldr_or_summary"),
                    "numeric_density_per_100w": fact_density.get("numeric_density_per_100w"),
                    "passages_30w_plus_without_numbers": fact_density.get("passages_30w_plus_without_numbers"),
                    "original_data_signal": fact_density.get("original_data_signal"),
                    "vague_authority_claims_count": fact_density.get("vague_authority_claims_count"),
                    "authoritative_outbound_links": fact_density.get("authoritative_outbound_links"),
                    "http_last_modified": raw.get("http_last_modified"),
                    "schema_date_published": raw.get("schema_date_published"),
                    "schema_date_modified": raw.get("schema_date_modified"),
                }
                if existing:
                    page_id = existing[0]
                    conn.execute(update(pages).where(pages.c.id == page_id).values(**page_values))
                else:
                    page_values["first_seen"] = now
                    page_id = conn.execute(insert(pages).values(**page_values)).inserted_primary_key[0]

                page_issues = [*report_issues, *content_issues]
                for mago_issue in page_issues:
                    created = record_issue(
                        conn,
                        project_id=self.project["id"],
                        snapshot_id=snapshot_id,
                        page_id=page_id,
                        issue=mago_issue,
                        now=now,
                    )
                    if created:
                        summary["issues_created"] += 1
                        summary["by_severity"][mago_issue.severity] += 1

                # Reconciliación: cierra issues viejas de ESTA página que este
                # análisis fresco ya no reporta (ej. un falso positivo corregido
                # o un problema arreglado en el sitio) — evita fantasmas 'open'.
                summary["issues_resolved"] += reconcile_page_issues(
                    conn,
                    project_id=self.project["id"],
                    page_id=page_id,
                    fresh_keys={(i.category, i.title) for i in page_issues},
                    now=now,
                )

                summary["pages_analyzed"] += 1

        return summary


def run_crawler(project_slug: str, max_pages: int | None = None) -> dict:
    crawler = TechnicalCrawler(project_slug, max_pages=max_pages)
    try:
        snapshot_id = crawler.run()

        with get_connection() as conn:
            from backend.db.schema import snapshots as snapshots_table

            row = conn.execute(
                select(snapshots_table.c.raw_data, snapshots_table.c.status).where(
                    snapshots_table.c.id == snapshot_id
                )
            ).first()

        if row is None or row[1] == "error":
            return {"snapshot_id": snapshot_id, "status": "error", "summary": None}

        raw_pages = (row[0] or {}).get("pages", [])
        summary = crawler.persist_analysis(snapshot_id, raw_pages)
        return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}
    finally:
        # El crawl terminó (ok o error): limpiar el progreso para que el
        # frontend deje de mostrar la barra de este proyecto.
        progress.clear(project_slug)


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Crawler técnico ejecutable de forma aislada (regla S7)")
    parser.add_argument("--site", required=True, help="slug del proyecto, ej: jc")
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    result = run_crawler(args.site, max_pages=args.max_pages)
    print(json.dumps(result, indent=2, ensure_ascii=False))
