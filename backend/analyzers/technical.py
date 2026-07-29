"""Reglas de semáforo técnico exactas (§6.4) y detección de bugs conocidos (§5.3.7).

Cada regla es una función pura testeable de forma aislada. `analyze_page` combina
todas las reglas y devuelve tanto el resumen semáforo (para la tabla) como la
lista de MagoIssue (para el Action Plan).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.analyzers.mago import MagoIssue

Semaphore = str  # "green" | "yellow" | "red"

_GREEN, _YELLOW, _RED = "green", "yellow", "red"

# Palabras funcionales comunes para la heurística de idioma (§5.3.7: contenido
# en inglés en sitio español). No es un detector de idioma real, es una señal.
_SPANISH_STOPWORDS = {
    "el", "la", "los", "las", "de", "en", "y", "para", "con", "por", "un",
    "una", "que", "es", "su", "del", "se", "no", "más", "cómo", "qué",
}
_ENGLISH_STOPWORDS = {
    "the", "and", "for", "with", "our", "your", "is", "are", "we", "you",
    "of", "to", "in", "at", "on",
}


@dataclass
class PageData:
    """Datos extraídos de una página por el crawler, listos para analizar."""

    url: str
    title: str | None = None
    meta_description: str | None = None
    h1_tags: list[str] = field(default_factory=list)
    h1_raw_html: str | None = None  # HTML crudo del/los H1, para detectar <br/>
    schema_types: list[str] = field(default_factory=list)
    schema_has_errors: bool = False
    og: dict = field(default_factory=dict)  # keys: title, description, image
    canonical: str | None = None
    is_indexable: bool = True
    noindex_justified: bool = False  # el usuario marcó explícitamente por qué
    lang_declared: str = "es"
    keyword: str | None = None
    brand: str | None = None
    x_robots_tag: str | None = None  # header HTTP crudo (§ herramientas de mercado 2026-07-24)


_DUPLICATE_SUFFIX_RE = re.compile(r"(?:\s*[|·\-–—]\s*)([^|·\-–—]{2,40}?)\s*[|·\-–—]\s*\1\s*$", re.IGNORECASE)


def detect_duplicate_brand_suffix(title: str | None) -> str | None:
    """Detecta '… | Marca | Marca' al final del título y devuelve la marca.

    Es un bug MUY común en Next.js/Gatsby/Nuxt: el layout define
    `title.template = '%s | Marca'` y la página además incluye '| Marca' en su
    string, así que el sufijo se duplica. Verificado en jcreparaciones.com,
    donde afectaba a las páginas dinámicas /reparacion/[marca]/[modelo]/... y a
    ~36 páginas marca×ciudad.

    Reportarlo aparte importa: "título de 80 chars" manda a recortar texto útil,
    cuando la causa real es un sufijo repetido — arreglar el template lo
    soluciona de golpe en todas las páginas (§ #3 del reporte de falsos
    positivos)."""
    if not title:
        return None
    match = _DUPLICATE_SUFFIX_RE.search(title)
    return match.group(1).strip() if match else None


def evaluate_title(title: str | None, keyword: str | None, brand: str | None) -> tuple[Semaphore, str]:
    if not title:
        return _RED, "Falta el title"
    # Antes que la longitud: si el sufijo de marca está duplicado, ESA es la
    # causa raíz del título largo y merece un mensaje accionable propio.
    dup_suffix = detect_duplicate_brand_suffix(title)
    if dup_suffix:
        return _RED, (
            f'Doble sufijo de marca: "{dup_suffix}" aparece dos veces al final del title '
            f"({len(title)} chars). Suele ser el template del layout (title.template) sumado "
            "al sufijo ya incluido en la página — arregla el template, no recortes el texto."
        )
    length = len(title)
    # La keyword/marca solo se puede EXIGIR cuando se conoce. En la práctica el
    # crawler no fija keyword por página (es None), así que penalizar "sin
    # keyword" convertía cada title de buena longitud en un falso ALTA (§ #6).
    keyword_known = keyword is not None
    has_keyword = keyword_known and keyword.lower() in title.lower()
    if length < 20 or length > 70:
        return _RED, f"Title de {length} chars, fuera de rango aceptable (20-70)"
    if 20 <= length < 30 or 60 < length <= 70:
        return _YELLOW, f"Title de {length} chars, en zona límite"
    # Longitud óptima (30-60): solo marcamos amarillo si la keyword SE CONOCE y
    # de verdad falta; si no la conocemos, no se inventa un problema.
    if keyword_known and not has_keyword:
        return _YELLOW, "Title sin la keyword principal"
    return _GREEN, "Title óptimo"


def evaluate_meta_description(desc: str | None) -> tuple[Semaphore, str]:
    if not desc:
        return _RED, "Falta la meta description"
    length = len(desc)
    has_cta = any(marker in desc for marker in ["→", "cotiza", "llama", "agenda", "compra", "$"])
    if length < 100:
        return _RED, f"Meta description de {length} chars, muy corta"
    if 120 <= length <= 160 and has_cta:
        return _GREEN, "Meta description óptima con CTA"
    if 100 <= length < 120 or 160 < length <= 180:
        return _YELLOW, f"Meta description de {length} chars, en zona límite"
    if not has_cta:
        return _YELLOW, "Meta description sin CTA claro"
    return _YELLOW, "Meta description aceptable pero mejorable"


_STUCK_WORDS_PATTERN = re.compile(r"[a-záéíóúñ][A-ZÁÉÍÓÚÑ]")

# Marcas legítimas en camelCase que NO deben confundirse con palabras pegadas
# (bug real es tipo "REPARACIÓNiPHONE", no la marca "iPhone" en sí misma).
# Se ordenan de más largo a más corto para que "iPadOS" gane sobre "iPad".
_KNOWN_BRAND_TOKENS = sorted(
    [
        "iPhone", "iPad", "iPadOS", "iPod", "iMac", "MacBook", "iOS", "macOS",
        "WhatsApp", "YouTube", "PayPal", "TikTok",
    ],
    key=len,
    reverse=True,
)
# \b...\b + re.IGNORECASE: estas marcas se escriben estilizadas en los H1
# ("iPHONE", "MACBOOK") — sin case-insensitive, "REPARACIÓN iPHONE EN CALI"
# disparaba un falso "palabras pegadas" por la 'i' minúscula + 'PHONE'
# mayúscula (§ #2, verificado en vivo en /reparacion-iphone-cali).
# El límite de palabra \b es clave: solo elimina la marca cuando está
# separada ("REPARACIÓN iPHONE", con espacio => es una marca legítima), pero
# NO cuando está pegada a otra palabra sin espacio ("REPARACIÓNiPHONE" no tiene
# frontera entre 'N' e 'i'), preservando la detección del pegado REAL.
_BRAND_STRIP_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(b) for b in _KNOWN_BRAND_TOKENS) + r")\b",
    re.IGNORECASE,
)


def detect_stuck_words(text: str) -> bool:
    """Detecta palabras pegadas típicas de un <br/> mal manejado: 'REPARACIÓNiPHONE'.

    Se excluyen marcas conocidas en camelCase legítimo (iPhone, iPad, etc.) para
    evitar falsos positivos sobre el nombre de un producto.
    """
    cleaned = _BRAND_STRIP_PATTERN.sub(" ", text)
    return bool(_STUCK_WORDS_PATTERN.search(cleaned))


def evaluate_h1(h1_tags: list[str], keyword: str | None) -> tuple[Semaphore, str]:
    if not h1_tags:
        return _RED, "Falta el H1"
    stuck = any(detect_stuck_words(h1) for h1 in h1_tags)
    if stuck:
        return _RED, "H1 con palabras pegadas (probable <br/> mal manejado)"
    if len(h1_tags) > 1:
        return _YELLOW, f"{len(h1_tags)} H1 encontrados, debería haber solo 1"
    # La keyword se evalúa en el <title>, NO en el H1 (§ #6): un H1 de marca
    # ("JC Reparaciones · …") es legítimo, no un error. Un H1 único y limpio
    # es verde. Si conocemos la keyword y aparece, lo decimos como refuerzo.
    if keyword is not None and keyword.lower() in h1_tags[0].lower():
        return _GREEN, "H1 único con keyword"
    return _GREEN, "H1 único"


_RELEVANT_SCHEMA_TYPES = {
    "LocalBusiness", "Service", "Product", "FAQPage", "HowTo", "Organization",
    "Article", "BlogPosting", "BreadcrumbList", "Review", "AggregateRating",
}


def evaluate_schema(schema_types: list[str], has_errors: bool) -> tuple[Semaphore, str]:
    if not schema_types:
        return _RED, "Sin schema JSON-LD"
    if has_errors:
        return _YELLOW, "Schema presente pero con errores de validación"
    relevant = [t for t in schema_types if t in _RELEVANT_SCHEMA_TYPES]
    if relevant:
        return _GREEN, f"Schema relevante: {', '.join(relevant)}"
    return _YELLOW, f"Schema genérico: {', '.join(schema_types)}"


def evaluate_og(og: dict) -> tuple[Semaphore, str]:
    if not og:
        return _RED, "Sin Open Graph"
    present = [k for k in ("title", "description", "image") if og.get(k)]
    if len(present) == 3:
        return _GREEN, "Open Graph completo"
    if present:
        missing = [k for k in ("title", "description", "image") if k not in present]
        return _YELLOW, f"Open Graph incompleto, falta: {', '.join(missing)}"
    return _RED, "Sin Open Graph"


def evaluate_canonical(canonical: str | None, page_url: str) -> tuple[Semaphore, str]:
    if not canonical:
        return _RED, "Falta canonical"
    page_has_www = "://www." in page_url
    canonical_has_www = "://www." in canonical
    if page_has_www != canonical_has_www:
        return _YELLOW, "Canonical con www residual (inconsistente con la URL)"
    return _GREEN, "Canonical presente y autoconsistente"


_BLOCKING_DIRECTIVES = {"noindex", "none"}


def parse_x_robots_directives(header_value: str | None) -> set[str]:
    """Parsea el header X-Robots-Tag en directivas normalizadas.

    Soporta el formato dirigido a un bot específico ('googlebot: noindex') —
    se toma la parte después de los ':' si el primer segmento no es en sí una
    directiva conocida (evita confundir 'max-snippet: 20' con un bot-name)."""
    if not header_value:
        return set()
    parts = [p.strip() for p in header_value.split(",") if p.strip()]
    directives: set[str] = set()
    for part in parts:
        if ":" in part:
            prefix, _, rest = part.partition(":")
            directives.add(prefix.strip().lower() if prefix.strip().lower() not in ("googlebot", "bingbot", "*") else rest.strip().lower())
        else:
            directives.add(part.lower())
    return directives


def evaluate_x_robots_tag(x_robots_tag: str | None, meta_says_indexable: bool) -> tuple[Semaphore, str]:
    """Detecta el conflicto silencioso: el header HTTP dice noindex/none pero el
    HTML (meta robots) no lo refleja — invisible mirando solo el código fuente,
    y Google prioriza el header sobre el meta tag cuando difieren. Sin header
    presente (caso normal, la mayoría de sitios no lo usa) no hay nada que
    evaluar — verde, no se inventa un problema."""
    directives = parse_x_robots_directives(x_robots_tag)
    if not directives:
        return _GREEN, "Sin X-Robots-Tag (no aplica)"
    blocking = directives & _BLOCKING_DIRECTIVES
    if blocking and meta_says_indexable:
        return _RED, (
            f'X-Robots-Tag: "{x_robots_tag}" bloquea indexación por HEADER HTTP, '
            "pero el <meta name=\"robots\"> del HTML no lo refleja — Google prioriza el "
            "header, así que la página NO se indexa aunque el código fuente parezca normal."
        )
    if blocking:
        return _YELLOW, f'X-Robots-Tag: "{x_robots_tag}" — coincide con el noindex ya declarado en el HTML'
    return _GREEN, f'X-Robots-Tag: "{x_robots_tag}" (sin directivas de bloqueo)'


def evaluate_indexability(is_indexable: bool, noindex_justified: bool) -> tuple[Semaphore, str]:
    if is_indexable:
        return _GREEN, "Indexable"
    if noindex_justified:
        return _YELLOW, "No indexable, pero justificado explícitamente"
    return _RED, "No indexable (noindex/robots.txt) sin justificación registrada"


def detect_language_mismatch(text: str, expected_lang: str) -> bool:
    """Heurística simple: cuenta stopwords ES vs EN en el texto (§5.3.7)."""
    if expected_lang != "es" or not text:
        return False
    words = set(re.findall(r"[a-záéíóúñ]+", text.lower()))
    es_hits = len(words & _SPANISH_STOPWORDS)
    en_hits = len(words & _ENGLISH_STOPWORDS)
    return en_hits > es_hits and en_hits >= 2


def detect_zombie_page(page: PageData) -> bool:
    """Página zombie (§5.3.7): sin H1, sin schema y con noindex."""
    return not page.h1_tags and not page.schema_types and not page.is_indexable


@dataclass
class TechnicalReport:
    row: dict[str, Semaphore]
    row_detail: dict[str, str]  # razón legible por celda, incluye las verdes (no solo issues)
    issues: list[MagoIssue]


def page_data_from_stored_row(page_row: dict) -> PageData:
    """Reconstruye un PageData desde una fila ya guardada en la tabla `pages`.

    Vive aquí (no en la capa de API) porque es lógica de análisis, no de
    transporte: tanto las rutas del dashboard como opportunities.py la usan
    para recalcular el semáforo sin volver a crawlear (analyze_page es
    determinista, así que `pages` es la única fuente de verdad).
    """
    return PageData(
        url=page_row["url"],
        title=page_row.get("title"),
        meta_description=page_row.get("meta_description"),
        h1_tags=(page_row.get("h1") or "").split(" | ") if page_row.get("h1") else [],
        schema_types=page_row.get("schema_types") or [],
        og=page_row.get("og") or {},
        canonical=page_row.get("canonical"),
        is_indexable=bool(page_row.get("is_indexable", True)),
        x_robots_tag=page_row.get("x_robots_tag"),
    )


def analyze_page(page: PageData) -> TechnicalReport:
    title_sem, title_reason = evaluate_title(page.title, page.keyword, page.brand)
    desc_sem, desc_reason = evaluate_meta_description(page.meta_description)
    h1_sem, h1_reason = evaluate_h1(page.h1_tags, page.keyword)
    schema_sem, schema_reason = evaluate_schema(page.schema_types, page.schema_has_errors)
    og_sem, og_reason = evaluate_og(page.og)
    canonical_sem, canonical_reason = evaluate_canonical(page.canonical, page.url)
    index_sem, index_reason = evaluate_indexability(page.is_indexable, page.noindex_justified)
    xrobots_sem, xrobots_reason = evaluate_x_robots_tag(page.x_robots_tag, page.is_indexable)

    row = {
        "title": title_sem,
        "meta_description": desc_sem,
        "h1": h1_sem,
        "schema": schema_sem,
        "og": og_sem,
        "canonical": canonical_sem,
        "indexable": index_sem,
        "x_robots": xrobots_sem,
    }
    row_detail = {
        "title": title_reason,
        "meta_description": desc_reason,
        "h1": h1_reason,
        "schema": schema_reason,
        "og": og_reason,
        "canonical": canonical_reason,
        "indexable": index_reason,
        "x_robots": xrobots_reason,
    }

    issues: list[MagoIssue] = []

    def _add(sem: Semaphore, category: str, reason: str, effort: str, impact: int) -> None:
        if sem == _GREEN:
            return
        # Calibración (bug real 2026-07-25): antes TODO amarillo se registraba
        # como severidad 'high' con el mismo impacto que su rojo. Resultado en
        # jcreparaciones.com: 159 issues de meta (105 con impacto 5/5, el mismo
        # peso que un noindex en una página clave) ahogando las 23 críticas
        # reales — el Action Plan dejaba de ser accionable. Un amarillo es
        # "mejorable", no "roto": baja a 'medium' y pesa menos.
        if sem == _RED:
            severity, effective_impact = "critical", impact
        else:
            severity, effective_impact = "medium", max(1, impact - 3)
        issues.append(
            MagoIssue(
                severity=severity,
                category=category,
                title=f"{page.url}: {reason}",
                page_url=page.url,
                effort=effort,
                impact=effective_impact,
            )
        )

    _add(title_sem, "meta", title_reason, "5min", 4)
    _add(desc_sem, "meta", desc_reason, "5min", 5)
    _add(h1_sem, "h1", h1_reason, "1h", 4)
    _add(schema_sem, "schema", schema_reason, "1h", 3)
    _add(og_sem, "og", og_reason, "1h", 2)
    _add(canonical_sem, "canonical", canonical_reason, "1h", 3)
    _add(index_sem, "index", index_reason, "1h", 5)
    # Solo el conflicto REAL (header bloquea, meta no lo refleja) genera issue —
    # el caso "coincide con el noindex ya declarado" (amarillo) es informativo
    # en la tabla pero duplicaría el issue de 'indexable' si se reportara aparte.
    if xrobots_sem == _RED:
        _add(xrobots_sem, "index", xrobots_reason, "5min", 5)

    combined_text = " ".join(filter(None, [page.title, page.meta_description, *page.h1_tags]))
    if detect_language_mismatch(combined_text, page.lang_declared):
        issues.append(
            MagoIssue(
                severity="critical",
                category="content",
                title=f"{page.url}: contenido en inglés en sitio en español",
                page_url=page.url,
                current=combined_text[:120],
                effort="1d",
                impact=4,
            )
        )

    if detect_zombie_page(page):
        issues.append(
            MagoIssue(
                severity="critical",
                category="zombie",
                title=f"{page.url}: página zombie (sin H1, sin schema, noindex)",
                page_url=page.url,
                effort="1h",
                impact=3,
            )
        )

    return TechnicalReport(row=row, row_detail=row_detail, issues=issues)
