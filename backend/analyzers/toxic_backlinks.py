"""Backlinks tóxicos por patrón, no por score pagado de terceros (§ mejoras
2026-08-26, spec del usuario tras la auditoría manual de jcreparaciones.com
— categoría 8 del prompt de 8 problemas).

Por qué NO es un prompt de LLM: `business_rules.py` hace pattern-matching de
texto contra una lista de frases — no es un LLM juzgando. Este módulo sigue
el mismo principio: las señales de toxicidad son reglas determinísticas
(regex, listas, comparación de dominio), no juicio subjetivo. Eso las hace
rápidas, gratis de correr a escala, y con el mismo test suite discipline que
el resto de SEO-OS. Un LLM entraría opcionalmente en el Nivel 2 (más abajo)
solo para el caso ambiguo que ninguna regla resuelve — y ese nivel está
apagado por defecto.

Fuente de los `Backlink`: exportable desde Semrush (CSV/API), GSC (Links
report), Bing Webmaster Tools, o carga manual — el módulo no depende de un
proveedor específico, solo de este contrato de datos (regla del proyecto:
no atarse a una sola API de pago).

Honestidad (P1), mismo criterio que `calculate_share_of_voice`: con pocos
backlinks, un "% tóxico" no dice nada — la única llamada honesta es negarse
a calcularlo, no inventar un número sobre una muestra de 3 enlaces."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable, Literal
from urllib.parse import urlparse

from backend.analyzers.backlinks import generate_disavow_file

MIN_BACKLINKS_FOR_SCORE = 10


@dataclass
class Backlink:
    source_domain: str
    source_url: str
    anchor_text: str
    target_url: str
    link_type: Literal["follow", "nofollow"]
    authority_score: int | None = None
    first_seen: date | None = None


@dataclass
class Signal:
    name: str
    classification: Literal["toxic", "review"]
    reason: str


@dataclass
class BacklinkVerdict:
    backlink: Backlink
    classification: Literal["toxic", "review", "clean"]
    signals: list[str] = field(default_factory=list)
    recommended_action: str = "No tocar"


@dataclass
class ToxicBacklinksReport:
    available: bool
    reason: str | None
    total_backlinks: int
    toxic_count: int
    review_count: int
    clean_count: int
    toxic_pct: float | None
    verdicts: list[BacklinkVerdict]
    disavow_file_content: str


# ---------- Nivel 1: señales determinísticas, sin red ----------

TOXIC_ANCHOR_PATTERNS = [
    r"\bbuy backlinks\b", r"\bcheap seo\b", r"\bpbn\b",
    r"\bdofollow backlinks?\b", r"\bda\s*\d+\s*pa\s*\d+\b",
    r"\bpremium backlinks?\b", r"\blink building service\b",
    r"\brank first page google\b", r"\baged domains?\b",
]
_TOXIC_ANCHOR_RE = re.compile("|".join(TOXIC_ANCHOR_PATTERNS), re.IGNORECASE)

TOXIC_TLDS = {"sbs", "cfd", "xyz", "top", "click", "cv", "loan", "work"}

TOXIC_URL_PATH_PATTERNS = [
    r"/[a-z]{6,}seo/",           # rutas tipo /iiklhseo/, /jsjjhseo/
    r"directory-pages-index",
    r"seo-service.*-best-seo",
]
_TOXIC_URL_PATH_RE = re.compile("|".join(TOXIC_URL_PATH_PATTERNS), re.IGNORECASE)

REVIEW_DOMAIN_SUFFIXES = {"blogspot.com", "wordpress.com", "wixsite.com"}

# Anchors sin ningún valor descriptivo — típicos de directorios masivos
# generados en volumen. No es prueba de spam (un directorio real también los
# usa), por eso es señal de REVISAR, nunca de tóxico directo.
_GENERIC_ANCHOR_PHRASES = {
    "click here", "clic aquí", "clic aqui", "leer más", "leer mas",
    "sitio web", "página web", "pagina web", "aquí", "aqui", "here",
    "website", "más información", "mas informacion", "more info",
}


def _tld(domain: str) -> str:
    parts = (domain or "").rsplit(".", 1)
    return parts[-1].lower() if len(parts) == 2 else ""


def signal_toxic_anchor(bl: Backlink) -> Signal | None:
    if bl.anchor_text and _TOXIC_ANCHOR_RE.search(bl.anchor_text):
        return Signal(
            name="toxic_anchor",
            classification="toxic",
            reason=f'Anchor text de venta de enlaces: "{bl.anchor_text}"',
        )
    return None


def signal_toxic_tld(bl: Backlink) -> Signal | None:
    tld = _tld(bl.source_domain)
    if tld in TOXIC_TLDS:
        return Signal(
            name="toxic_tld",
            classification="toxic",
            reason=f'TLD ".{tld}" asociado a redes de enlaces baratas (heurística débil, sin certeza)',
        )
    return None


def signal_toxic_url_pattern(bl: Backlink) -> Signal | None:
    path = urlparse(bl.source_url or "").path
    if path and _TOXIC_URL_PATH_RE.search(path):
        return Signal(
            name="toxic_url_pattern",
            classification="toxic",
            reason=f'Ruta de URL con patrón típico de granja de enlaces: "{path}"',
        )
    return None


def signal_review_free_platform(bl: Backlink) -> Signal | None:
    domain = (bl.source_domain or "").lower()
    for suffix in REVIEW_DOMAIN_SUFFIXES:
        if domain == suffix or domain.endswith(f".{suffix}"):
            return Signal(
                name="review_free_platform",
                classification="review",
                reason=f'Dominio en plataforma gratuita ({domain}) — puede ser legítimo o spam, requiere revisión manual',
            )
    return None


def signal_review_generic_directory(bl: Backlink) -> Signal | None:
    anchor = (bl.anchor_text or "").strip().lower()
    if not anchor:
        return None
    if anchor in _GENERIC_ANCHOR_PHRASES or anchor == (bl.source_domain or "").lower():
        return Signal(
            name="review_generic_directory",
            classification="review",
            reason=f'Anchor text genérico/no descriptivo ("{bl.anchor_text}") — típico de directorios masivos, revisar si aporta valor real',
        )
    return None


LEVEL_1_TOXIC_SIGNALS = [signal_toxic_anchor, signal_toxic_tld, signal_toxic_url_pattern]
LEVEL_1_REVIEW_SIGNALS = [signal_review_free_platform, signal_review_generic_directory]


# ---------- Nivel 2: opcional, requiere fetch, apagado por defecto ----------

def signal_domain_sells_links(bl: Backlink, fetch_fn: Callable[[str], str | None]) -> Signal | None:
    """Solo se llama cuando config.enable_domain_content_check=True. Descarga
    la home del source_domain y busca las mismas frases de TOXIC_ANCHOR_PATTERNS
    en su propio contenido (no en el anchor puntual, en el body) — detecta
    sitios que venden enlaces aunque el anchor de ESTE backlink sea inofensivo.
    `fetch_fn` se inyecta (nunca red real en tests, regla QA) y se debe cachear
    por dominio en el caller — ver apply_domain_content_check()."""
    try:
        body = fetch_fn(f"https://{bl.source_domain}")
    except Exception:  # noqa: BLE001 - un fetch que falla no es evidencia de nada, se ignora
        return None
    if body and _TOXIC_ANCHOR_RE.search(body):
        return Signal(
            name="domain_sells_links",
            classification="toxic",
            reason=f"La home de {bl.source_domain} contiene frases de venta de enlaces en su propio contenido",
        )
    return None


def apply_domain_content_check(
    verdicts: list[BacklinkVerdict], fetch_fn: Callable[[str], str | None]
) -> list[BacklinkVerdict]:
    """Paso separado, bajo demanda — nunca corre en el pipeline estándar de
    auditoría (evita depender de red/latencia ahí, mismo criterio que la
    comparación detallada de competidor). Cachea por dominio: un dominio con
    5 backlinks se fetchea una sola vez, no 5."""
    cache: dict[str, Signal | None] = {}
    updated: list[BacklinkVerdict] = []
    for v in verdicts:
        domain = v.backlink.source_domain
        if domain not in cache:
            cache[domain] = signal_domain_sells_links(v.backlink, fetch_fn)
        sig = cache[domain]
        if sig is None or sig.name in v.signals:
            updated.append(v)
            continue
        updated.append(
            replace(
                v,
                classification="toxic",
                signals=[*v.signals, sig.name],
                recommended_action=_recommended_action("toxic", []),
            )
        )
    return updated


# ---------- Clasificación ----------

def _recommended_action(classification: str, signals: list[Signal]) -> str:
    if classification == "toxic":
        return "Agregar a disavow"
    if classification == "review":
        reasons = "; ".join(s.reason for s in signals if s.classification == "review")
        return f"Verificar: {reasons}" if reasons else "Verificar manualmente"
    return "No tocar"


def classify_backlink(bl: Backlink, config: dict | None = None) -> BacklinkVerdict:
    config = config or {}

    # El whitelist gana SIEMPRE — sin esto, cualquier directorio de negocios
    # local legítimo con baja autoridad (o un TLD barato usado por un negocio
    # real) terminaría marcado igual que un PBN. Nunca auto-disavow sin
    # permitir excepciones por proyecto.
    whitelist = set(config.get("domain_whitelist") or [])
    if bl.source_domain in whitelist:
        return BacklinkVerdict(
            backlink=bl,
            classification="clean",
            signals=[],
            recommended_action="No tocar (dominio en whitelist del proyecto)",
        )

    fired: list[Signal] = list(filter(None, (fn(bl) for fn in LEVEL_1_TOXIC_SIGNALS)))

    extra_patterns = config.get("extra_toxic_anchor_patterns") or []
    if extra_patterns and bl.anchor_text:
        extra_re = re.compile("|".join(extra_patterns), re.IGNORECASE)
        if extra_re.search(bl.anchor_text):
            fired.append(
                Signal(
                    name="toxic_anchor_custom",
                    classification="toxic",
                    reason=f'Anchor coincide con patrón adicional configurado por el proyecto: "{bl.anchor_text}"',
                )
            )

    fired.extend(filter(None, (fn(bl) for fn in LEVEL_1_REVIEW_SIGNALS)))

    has_toxic = any(s.classification == "toxic" for s in fired)
    has_review = any(s.classification == "review" for s in fired)

    if has_toxic and bl.link_type == "nofollow":
        # nofollow no pasa autoridad — un dominio que PARECE spam pero no
        # puede transmitir penalización de enlaces es un riesgo real menor.
        # Se baja a 'review' (sigue valiendo la pena mirarlo), no se
        # prioriza para disavow como uno follow con las mismas señales.
        classification: Literal["toxic", "review", "clean"] = "review"
    elif has_toxic:
        classification = "toxic"
    elif has_review:
        classification = "review"
    else:
        classification = "clean"

    return BacklinkVerdict(
        backlink=bl,
        classification=classification,
        signals=[s.name for s in fired],
        recommended_action=_recommended_action(classification, fired),
    )


def toxic_backlinks_report(backlinks: list[Backlink], config: dict | None = None) -> ToxicBacklinksReport:
    config = config or {}

    if len(backlinks) < MIN_BACKLINKS_FOR_SCORE:
        return ToxicBacklinksReport(
            available=False,
            reason=(
                f"Solo {len(backlinks)} backlink(s) cargado(s): no alcanza para un score confiable "
                f"(mínimo {MIN_BACKLINKS_FOR_SCORE}). Un % tóxico sobre una muestra tan chica no dice nada."
            ),
            total_backlinks=len(backlinks),
            toxic_count=0,
            review_count=0,
            clean_count=0,
            toxic_pct=None,
            verdicts=[],
            disavow_file_content="",
        )

    verdicts = [classify_backlink(bl, config) for bl in backlinks]
    toxic = [v for v in verdicts if v.classification == "toxic"]
    review = [v for v in verdicts if v.classification == "review"]
    clean = [v for v in verdicts if v.classification == "clean"]

    disavow_content = generate_disavow_file(
        [{"source_domain": v.backlink.source_domain, "reason": ", ".join(v.signals) or "sin detalle"} for v in toxic]
    )

    return ToxicBacklinksReport(
        available=True,
        reason=None,
        total_backlinks=len(backlinks),
        toxic_count=len(toxic),
        review_count=len(review),
        clean_count=len(clean),
        toxic_pct=round(100 * len(toxic) / len(backlinks), 1),
        verdicts=verdicts,
        disavow_file_content=disavow_content,
    )
