"""Comparador del top-10 real de una keyword (§ mejoras 2026-07-25): mide las
páginas que Google pone arriba y las contrasta con la nuestra.

Por qué hace falta: el keyword gap existente compara contra competidores
REGISTRADOS A MANO y su contenido general. Esto compara contra quien Google
realmente premia PARA ESA KEYWORD, que es la pregunta que hace todo cliente
("¿por qué ellos y no yo?").

Honestidad (P1) — esto es lo más importante de este módulo: se reportan
DIFERENCIAS MEDIDAS, nunca causalidad. Que las páginas del top-10 tengan
1.800 palabras y la nuestra 400 es un hecho; decir "rankean POR eso" sería
inventar. Google usa cientos de señales que no podemos observar (autoridad
de dominio, enlaces, comportamiento de usuario, historial). El texto que se
expone al usuario dice "diferencias medibles", no "causas".

Ético (P5): mismo User-Agent identificable, 1 req/s, respeta robots.txt de
cada sitio ajeno, tope duro de páginas, guard SSRF antes de cada fetch.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import statistics
import time
from collections import Counter
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from backend.analyzers.url_safety import UnsafeURLError, validate_public_url
from backend.collectors.crawler import (
    MAX_BODY_BYTES,
    MIN_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _extract_page_data,
    _normalize_url,
)
from backend.db.database import get_connection
from backend.db.schema import projects, serp_results

logger = logging.getLogger(__name__)

DEFAULT_MAX_URLS = 5  # 5 páginas ajenas a 1 req/s ≈ 5-10s reales

# § mejoras 2026-08-25 (pedido del usuario tras revisar Seobility — su
# herramienta TF*IDF): términos que comparten las páginas del top-10 real y
# nos faltan a nosotros. Lista de stopwords en español — palabras function
# sin valor temático, para no llenar el resultado de "de", "para", "que".
_STOPWORDS_ES = frozenset("""
de la que el en y a los del se las por un para con no una su al lo como más
pero sus le ya o este sí porque esta entre cuando muy sin sobre también me
hasta hay donde quien desde todo nos durante todos uno les ni contra otros
ese eso ante ellos e esto mí antes algunos qué unos yo otro otras otra él
tanto esa estos mucho quienes nada muchos cual poco ella estar estas
algunas algo nosotros mi mis tú te ti tu tus ellas nosotras vosotros
vosotras os mío mía míos mías tuyo tuya tuyos tuyas suyo suya suyos suyas
nuestro nuestra nuestros nuestras vuestro vuestra vuestros vuestras esos
esas estoy esta estamos estáis están esté estés estemos estéis estén
estaré estarás estará estaremos estaréis estarán era eras éramos erais
eran fui fue fuimos fuisteis fueron soy eres somos sois son sea seas
seamos seáis sean será serás seremos seréis serán tiene tienen tengo
tienes tenemos tenéis han ha hemos habéis hube hubo hubimos hubieron sido
siendo puede pueden puedo puedes podemos podéis debe deben debo debes
sólo solo cada cómo así también aquí ahí allí muy sin embargo cuál cuáles
""".split())

_TERM_RE = re.compile(r"[a-záéíóúñü]{4,}", re.IGNORECASE)
MIN_PAGES_FOR_TERM_GAP = 2  # el término debe aparecer en ≥2 páginas del top-10, no en una sola (ruido)
MAX_TERM_GAPS = 15


def _extract_terms(text: str) -> list[str]:
    return [w.lower() for w in _TERM_RE.findall(text or "") if w.lower() not in _STOPWORDS_ES]


def _compute_term_gaps(our_text: str, competitor_texts: list[str], seed_keyword: str) -> list[dict]:
    """TF-IDF real, simplificado a lo comparable: términos que varias páginas
    del top-10 REAL comparten y que nuestra página no usa (o casi no usa).

    Honestidad (P1) — el mismo límite que el resto de este módulo: esto es
    vocabulario COMPARTIDO entre páginas que ya rankean, una correlación de
    cobertura temática — nunca una causa de posicionamiento. Google no
    "puntúa" coincidencia de TF-IDF; usar estos términos puede reflejar
    cobertura más completa del tema, no garantiza nada."""
    if not competitor_texts:
        return []

    n_docs = len(competitor_texts)
    doc_term_counts = [Counter(_extract_terms(t)) for t in competitor_texts]
    doc_freq: Counter = Counter()
    for counts in doc_term_counts:
        doc_freq.update(counts.keys())

    our_counts = Counter(_extract_terms(our_text))
    seed_words = set(_extract_terms(seed_keyword))  # obvio que debe aparecer, no es un "gap"

    scored = []
    for term, df in doc_freq.items():
        if term in seed_words or df < MIN_PAGES_FOR_TERM_GAP:
            continue
        idf = math.log(n_docs / df)
        tfidfs = []
        for counts in doc_term_counts:
            total = sum(counts.values())
            if total and term in counts:
                tfidfs.append((counts[term] / total) * idf)
        if not tfidfs:
            continue
        scored.append({
            "term": term,
            "pages_using_it": df,
            "pages_total": n_docs,
            "avg_tfidf": round(sum(tfidfs) / len(tfidfs), 4),
            "our_frequency": our_counts.get(term, 0),
        })

    gaps = [s for s in scored if s["our_frequency"] == 0]
    gaps.sort(key=lambda s: (-s["pages_using_it"], -s["avg_tfidf"]))
    return gaps[:MAX_TERM_GAPS]


def _fetch_and_measure(client: httpx.Client, url: str) -> dict | None:
    """Devuelve métricas objetivas de una página ajena, o None si no se pudo
    medir (robots, SSRF, no-HTML, error de red). Nunca lanza: una página que
    no se puede leer no debe tumbar la comparación completa (S3)."""
    try:
        safe_url = validate_public_url(url)
    except UnsafeURLError as exc:
        logger.info("SERP compare: %s bloqueada por guard SSRF (%s)", url, exc)
        return None

    normalized = _normalize_url(safe_url)
    try:
        robots_resp = client.get(urljoin(normalized, "/robots.txt"))
        if robots_resp.status_code == 200:
            robots = RobotFileParser()
            robots.parse(robots_resp.text.splitlines())
            if not robots.can_fetch(USER_AGENT, normalized):
                logger.info("SERP compare: robots.txt de %s prohíbe el análisis", normalized)
                return None
    except httpx.HTTPError:
        pass  # sin robots.txt legible se procede: es UNA página puntual, no un crawl

    try:
        response = client.get(normalized)
    except httpx.HTTPError as exc:
        logger.info("SERP compare: no se pudo obtener %s (%s)", normalized, exc)
        return None

    if "text/html" not in response.headers.get("content-type", ""):
        return None
    if len(response.content) > MAX_BODY_BYTES:
        return None

    page = _extract_page_data(normalized, response.status_code, response.text)
    # Texto completo para el análisis de términos (§ mejoras 2026-08-25) — NO
    # el body_text_sample truncado a ~5000 chars que usa legibilidad, porque
    # un artículo largo del top-10 perdería cobertura de términos reales si
    # se corta a la mitad. Ya tenemos el HTML en memoria, no cuesta red extra.
    #
    # Bug real encontrado verificando en vivo contra jc: sin quitar
    # nav/header/footer/aside, el "gap" de términos salía lleno de boilerplate
    # de plantilla ("síguenos", "contáctanos", horario) que 2 de 3 páginas
    # comparten por usar un template parecido — no por cobertura temática
    # real. Se quita antes de extraer texto, igual que M11/M12.
    #
    # Límite honesto que queda (P1, no resuelto — verificado en vivo): sitios
    # que ponen horario/dirección fuera de <header>/<footer>/<aside> (widgets
    # sin HTML5 semántico) pueden colar palabras tipo "cerrado"/"sábado" como
    # si fueran término de contenido. Resolverlo de verdad requeriría un
    # extractor de "contenido principal" (tipo Mozilla Readability), fuera de
    # alcance de este pase — se documenta el límite en vez de fingir que no existe.
    text_soup = BeautifulSoup(response.text, "lxml")
    for tag in text_soup.find_all(("nav", "header", "footer", "aside", "script", "style")):
        tag.decompose()
    full_text = text_soup.get_text(separator=" ", strip=True)
    return {
        "url": normalized,
        "word_count": page.word_count,
        "title_length": len(page.title or ""),
        "meta_length": len(page.meta_description or ""),
        "h1_count": len(page.h1_tags),
        "schema_types": page.schema_types,
        "has_schema": bool(page.schema_types),
        "has_author": page.has_author,
        "has_date": page.has_date,
        "has_contact": page.has_contact,
        "internal_links_count": len(page.internal_links),
        "full_text": full_text,
    }


def _summarize(measured: list[dict]) -> dict:
    """Medianas del top-10. Mediana y no promedio: una sola página gigante no
    debe desplazar la referencia de todo el grupo."""
    if not measured:
        return {}
    return {
        "pages_measured": len(measured),
        "median_word_count": int(statistics.median(m["word_count"] for m in measured)),
        "median_title_length": int(statistics.median(m["title_length"] for m in measured)),
        "median_h1_count": int(statistics.median(m["h1_count"] for m in measured)),
        "median_internal_links": int(statistics.median(m["internal_links_count"] for m in measured)),
        "pct_with_schema": round(sum(1 for m in measured if m["has_schema"]) / len(measured), 2),
        "pct_with_author": round(sum(1 for m in measured if m["has_author"]) / len(measured), 2),
        "pct_with_date": round(sum(1 for m in measured if m["has_date"]) / len(measured), 2),
        "common_schema_types": sorted(
            {t for m in measured for t in m["schema_types"]}
        ),
    }


def _build_differences(ours: dict | None, summary: dict) -> list[dict]:
    """Diferencias medidas nuestra-página vs mediana del top-10. Cada entrada
    es un HECHO comparado, sin veredicto causal."""
    if not ours or not summary:
        return []

    diffs = []

    ours_wc, top_wc = ours["word_count"], summary["median_word_count"]
    if top_wc and ours_wc < top_wc * 0.6:
        diffs.append({
            "metric": "Extensión de contenido",
            "ours": f"{ours_wc} palabras",
            "top10": f"{top_wc} palabras (mediana)",
            "note": "Tu página es notablemente más corta que la mediana del top-10.",
        })
    elif top_wc and ours_wc > top_wc * 1.8:
        diffs.append({
            "metric": "Extensión de contenido",
            "ours": f"{ours_wc} palabras",
            "top10": f"{top_wc} palabras (mediana)",
            "note": "Tu página es bastante más larga que la mediana del top-10 (más no siempre es mejor).",
        })

    if not ours["has_schema"] and summary["pct_with_schema"] >= 0.5:
        diffs.append({
            "metric": "Datos estructurados (schema)",
            "ours": "sin schema",
            "top10": f"{int(summary['pct_with_schema'] * 100)}% del top-10 sí tiene",
            "note": f"Tipos más vistos arriba: {', '.join(summary['common_schema_types'][:5]) or 'n/d'}.",
        })

    if not ours["has_author"] and summary["pct_with_author"] >= 0.5:
        diffs.append({
            "metric": "Autoría visible (E-E-A-T)",
            "ours": "sin autor detectado",
            "top10": f"{int(summary['pct_with_author'] * 100)}% del top-10 sí lo muestra",
            "note": "Señal de experiencia/autoridad que el top-10 mayoritariamente sí expone.",
        })

    if not ours["has_date"] and summary["pct_with_date"] >= 0.5:
        diffs.append({
            "metric": "Fecha de publicación/actualización",
            "ours": "sin fecha detectada",
            "top10": f"{int(summary['pct_with_date'] * 100)}% del top-10 sí la muestra",
            "note": "Relevante sobre todo en temas donde la frescura importa.",
        })

    if ours["h1_count"] == 0:
        diffs.append({
            "metric": "H1",
            "ours": "sin H1",
            "top10": f"mediana de {summary['median_h1_count']}",
            "note": "Falta el encabezado principal de la página.",
        })

    return diffs


def compare_serp_for_keyword(project_slug: str, keyword: str, max_urls: int = DEFAULT_MAX_URLS) -> dict:
    """Mide el top-N real de `keyword` y lo contrasta con nuestra página.

    No es un BaseCollector porque no produce un snapshot por proyecto: es una
    consulta puntual bajo demanda sobre una keyword concreta (mismo criterio
    que quick_analysis.py).
    """
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
        if project is None:
            raise ValueError(f"Proyecto '{project_slug}' no existe")

        latest_date = conn.execute(
            select(func.max(serp_results.c.date)).where(
                serp_results.c.project_id == project.id, serp_results.c.keyword == keyword
            )
        ).scalar()
        if not latest_date:
            return {
                "keyword": keyword,
                "available": False,
                "empty_reason": "Sin top-10 guardado para esta keyword — ejecuta 'Verificar ranking real' primero.",
            }

        rows = [
            dict(r._mapping)
            for r in conn.execute(
                select(serp_results)
                .where(
                    serp_results.c.project_id == project.id,
                    serp_results.c.keyword == keyword,
                    serp_results.c.date == latest_date,
                )
                .order_by(serp_results.c.position)
            ).all()
        ]

    our_row = next((r for r in rows if r["is_ours"]), None)
    competitor_rows = [r for r in rows if not r["is_ours"]][:max_urls]

    measured: list[dict] = []
    ours_measured: dict | None = None
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for i, row in enumerate(competitor_rows):
            if i > 0:
                time.sleep(MIN_DELAY_SECONDS)
            result = _fetch_and_measure(client, row["url"])
            if result:
                result.update({"position": row["position"], "domain": row["domain"], "title": row["title"]})
                measured.append(result)

        # Nuestra página: la que rankea si aparecemos; si no, la home del
        # proyecto — se declara cuál se usó para no confundir al leer el diff.
        our_url = our_row["url"] if our_row else project.url
        time.sleep(MIN_DELAY_SECONDS)
        ours_measured = _fetch_and_measure(client, our_url)

    summary = _summarize(measured)
    term_gaps = _compute_term_gaps(
        our_text=(ours_measured or {}).get("full_text", ""),
        competitor_texts=[m["full_text"] for m in measured],
        seed_keyword=keyword,
    )
    # full_text solo se necesitaba para el cálculo de arriba — no tiene sentido
    # mandarlo al frontend/reporte (payload grande, nadie lo muestra crudo).
    for m in measured:
        m.pop("full_text", None)
    if ours_measured:
        ours_measured.pop("full_text", None)

    return {
        "keyword": keyword,
        "available": True,
        "date": latest_date,
        "our_position": our_row["position"] if our_row else None,
        "our_url_measured": ours_measured["url"] if ours_measured else None,
        "our_url_is_ranking_page": our_row is not None,
        "ours": ours_measured,
        "top10_summary": summary,
        "competitors": measured,
        "differences": _build_differences(ours_measured, summary),
        "term_gaps": term_gaps,
        "disclaimer": (
            "Son diferencias MEDIDAS entre tu página y las del top-10, no causas de "
            "posicionamiento: Google usa señales que no podemos observar (enlaces, "
            "autoridad, comportamiento de usuario). Los términos de 'term_gaps' son "
            "vocabulario COMPARTIDO entre páginas que ya rankean (TF-IDF sobre el top-10 "
            "real) — una correlación de cobertura temática, no una causa: Google no "
            "puntúa coincidencia de términos, y usarlos no garantiza nada."
        ),
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Comparador del top-10 real de una keyword (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--max-urls", type=int, default=DEFAULT_MAX_URLS)
    args = parser.parse_args()

    print(json.dumps(compare_serp_for_keyword(args.site, args.keyword, args.max_urls), indent=2, ensure_ascii=False))
