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

from backend.analyzers.content import EeatSignals, build_content_issues, calculate_eeat_score, flesch_huerta_score
from backend.analyzers.issue_store import reconcile_page_issues, record_issue
from backend.analyzers.technical import PageData, analyze_page
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
    body_text_sample: str = ""  # primeros ~5000 chars, para legibilidad (§9 Fase 1)
    has_author: bool = False
    has_date: bool = False
    has_contact: bool = False
    local_business_schema: dict | None = None  # name/telephone del JSON-LD LocalBusiness (§9 Fase 4)
    schema_nodes: list[dict] = field(default_factory=list)  # {types, fields} para validar Rich Results (§ #5)


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
                    }
                )
    return nodes


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


def _extract_page_data(url: str, status_code: int, html: str) -> CrawledPage:
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
                page = _extract_page_data(url, response.status_code, response.text)
                # response.history no está vacío cuando httpx siguió una o más
                # redirecciones: la URL enlazada internamente NO es la final.
                # Enlazar directo al destino ahorra un salto (§ cobertura).
                if response.history:
                    page.redirected_to = str(response.url)
                page.x_robots_tag = response.headers.get("x-robots-tag")
                page.content_encoding = response.headers.get("content-encoding")
                page.cache_control = response.headers.get("cache-control")
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
        crawled_pages, errors = crawl_site(
            self.project["url"],
            self.max_pages,
            on_progress=lambda done, queued, url: progress.update(slug, done, url),
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

        with get_connection() as conn:
            for raw in raw_pages:
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
                    lang_declared=expected_lang,
                    x_robots_tag=raw.get("x_robots_tag"),
                )
                report = analyze_page(page_data)

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

                existing = conn.execute(
                    select(pages.c.id).where(
                        pages.c.project_id == self.project["id"], pages.c.url == raw["url"]
                    )
                ).first()
                now = now_iso()
                page_values = {
                    "project_id": self.project["id"],
                    "url": raw["url"],
                    "last_crawled": now,
                    "status_code": raw.get("status_code"),
                    "title": raw.get("title"),
                    "meta_description": raw.get("meta_description"),
                    "h1": " | ".join(raw.get("h1_tags", [])) or None,
                    "canonical": raw.get("canonical"),
                    "robots_meta": None,
                    "schema_types": raw.get("schema_types", []),
                    "og": raw.get("og", {}),
                    "word_count": raw.get("word_count"),
                    "lang_detected": raw.get("lang_declared"),
                    "is_indexable": raw.get("is_indexable", True),
                    "x_robots_tag": raw.get("x_robots_tag"),
                    "redirected_to": raw.get("redirected_to"),
                    "readability_score": round(readability_score),
                    "eeat_score": eeat_score,
                    "has_author": eeat_signals.has_author,
                    "has_date": eeat_signals.has_date,
                    "has_contact": eeat_signals.has_contact,
                }
                if existing:
                    page_id = existing[0]
                    conn.execute(update(pages).where(pages.c.id == page_id).values(**page_values))
                else:
                    page_values["first_seen"] = now
                    page_id = conn.execute(insert(pages).values(**page_values)).inserted_primary_key[0]

                page_issues = [*report.issues, *content_issues]
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
