"""Definición de todas las tablas (§3 del PROMPT_MAESTRO). SQLAlchemy 2.x Core.

Se usa Core (no ORM) a propósito: facilita migrar a PostgreSQL más adelante
sin reescribir modelos de clase.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

# Almacén KV de secretos configurados desde la UI (§ mejoras 2026-07-26): un
# valor aquí GANA sobre el .env — permite que alguien que se autoaloje esta
# app pegue sus API keys desde una pantalla, sin editar archivos. Si la fila
# no existe para una key, se usa el valor de .env (ver backend/settings_store.py).
# Vive en texto plano, igual que .env — este proyecto es local-first (regla S8:
# solo bind 127.0.0.1 sin AUTH_TOKEN), el mismo modelo de amenaza que ya asume
# el archivo .env de toda la vida.
app_settings = Table(
    "app_settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", String, nullable=False),
)

projects = Table(
    "projects",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", String, unique=True, nullable=False),
    Column("name", String, nullable=False),
    Column("url", String, nullable=False),
    Column("gsc_property", String, nullable=False),
    Column("country", String, default="CO"),
    Column("language", String, default="es"),
    Column("competitors", JSON, default=list),
    Column("is_active", Boolean, default=True),
    Column("config", JSON, default=dict),
    Column("created_at", String, nullable=False),
)

snapshots = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("collector", String, nullable=False),
    Column("status", String, nullable=False),  # ok|error|partial
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("error_message", Text),
    Column("raw_data", JSON),
    Column("created_at", String, nullable=False),
)

gsc_daily = Table(
    "gsc_daily",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("date", String, nullable=False),
    Column("clicks", Integer, default=0),
    Column("impressions", Integer, default=0),
    Column("ctr", Float, default=0.0),
    Column("position", Float, default=0.0),
    Column("created_at", String, nullable=False),
    UniqueConstraint("project_id", "date", name="uq_gsc_daily"),
)

gsc_queries = Table(
    "gsc_queries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("date", String, nullable=False),
    Column("query", String, nullable=False),
    Column("page", String),
    Column("clicks", Integer, default=0),
    Column("impressions", Integer, default=0),
    Column("ctr", Float, default=0.0),
    Column("position", Float, default=0.0),
    Column("created_at", String, nullable=False),
    UniqueConstraint("project_id", "date", "query", "page", name="uq_gsc_queries"),
)

pages = Table(
    "pages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("url", String, nullable=False),
    Column("first_seen", String, nullable=False),
    Column("last_crawled", String),
    Column("status_code", Integer),
    Column("title", Text),
    Column("meta_description", Text),
    Column("h1", Text),
    Column("canonical", Text),
    Column("robots_meta", String),
    Column("schema_types", JSON, default=list),
    Column("og", JSON, default=dict),
    Column("word_count", Integer),
    Column("lang_detected", String),
    Column("is_indexable", Boolean, default=True),
    # Columnas de Contenido & E-E-A-T (§9 Fase 1). Añadidas después del schema
    # original de Fase 0 — ver migrations.py para el ALTER TABLE idempotente
    # que las agrega a bases de datos que ya existían antes de Fase 1.
    Column("readability_score", Integer),
    Column("eeat_score", Integer),
    Column("has_author", Boolean),
    Column("has_date", Boolean),
    Column("has_contact", Boolean),
    # § herramientas de mercado 2026-07-24: header HTTP crudo (puede contradecir
    # el <meta name="robots"> del HTML — Google prioriza el header). None si el
    # servidor no lo manda (caso normal, la mayoría de sitios no lo usa).
    Column("x_robots_tag", Text),
    # § bug real 2026-07-24: antes solo vivía en el snapshot crudo del crawl
    # MÁS RECIENTE. Si una URL vieja con redirect no se re-visitaba en un
    # crawl posterior (ya nadie la enlaza, cae fuera de la muestra de 100
    # páginas), su fila en `pages` seguía con el título viejo sin marca de
    # redirect y "reaparecía" como duplicado — el mismo bug que ya habíamos
    # arreglado, pero por una vía distinta. Persistirlo aquí hace que la
    # cobertura de redirects SUME entre corridas en vez de resetear cada vez.
    Column("redirected_to", Text),
    UniqueConstraint("project_id", "url", name="uq_pages_project_url"),
)

issues = Table(
    "issues",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("page_id", Integer, ForeignKey("pages.id"), nullable=True),
    Column("snapshot_id", Integer, ForeignKey("snapshots.id"), nullable=True),
    Column("severity", String, nullable=False),  # critical|high|medium
    Column("category", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("current_text", Text),
    Column("suggested_text", Text),
    Column("effort", String),  # 5min|1h|1d
    Column("impact", Integer),  # 1-5
    Column("status", String, default="open"),  # open|done|dismissed|resolved (resolved = el detector dejó de verlo al re-analizar)
    Column("detected_at", String, nullable=False),
    Column("resolved_at", String),
)

scores = Table(
    "scores",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("date", String, nullable=False),
    Column("kind", String, nullable=False),  # seo|geo|eeat|technical
    Column("value", Integer, nullable=False),
    Column("breakdown", JSON, default=dict),
    UniqueConstraint("project_id", "date", "kind", name="uq_scores"),
)

keywords = Table(
    "keywords",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("keyword", String, nullable=False),
    Column("source", String, nullable=False),  # gsc|trends|manual
    Column("volume", Integer),
    Column("trend_data", JSON),
    Column("intent", String),
    Column("last_updated", String, nullable=False),
    UniqueConstraint("project_id", "keyword", "source", name="uq_keywords"),
)

backlinks = Table(
    "backlinks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("source_url", Text, nullable=False),  # página que enlaza (dominio referente)
    Column("source_domain", String, nullable=False),
    Column("target_url", Text, nullable=False),  # página propia enlazada
    Column("anchor_text", Text),
    Column("source", String, nullable=False),  # bing (única fuente activa hoy)
    Column("domain_authority", Integer),  # reservado por si una fuente futura lo reporta; hoy siempre None
    Column("spam_score", Integer),  # reservado por si una fuente futura lo reporta; hoy siempre None
    Column("is_toxic", Boolean, default=False),
    Column("first_seen", String, nullable=False),
    Column("last_seen", String, nullable=False),
    Column("status", String, default="active"),  # active|lost
    UniqueConstraint("project_id", "source_url", "target_url", "anchor_text", name="uq_backlinks"),
)

ai_messages = Table(
    "ai_messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("role", String, nullable=False),  # user|assistant
    Column("content", Text, nullable=False),
    Column("tokens_used", Integer, default=0),
    Column("cost_estimate", Float, default=0.0),
    Column("created_at", String, nullable=False),
)

pagespeed = Table(
    "pagespeed",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("date", String, nullable=False),
    Column("strategy", String, nullable=False),  # mobile|desktop
    # § herramientas de mercado 2026-07-24: antes esta tabla media SOLO la home
    # (1 fila por día+estrategia). Ahora es 1 fila por URL+día+estrategia — ver
    # migrations.py para el rebuild de tabla que preserva las filas viejas
    # (backfill con la home, que es lo que realmente medían).
    Column("url", Text, nullable=False),
    # Scores Lighthouse 0-100 (lab data, simulado — cualquier URL las tiene).
    Column("performance_score", Integer),
    Column("accessibility_score", Integer),
    Column("best_practices_score", Integer),
    Column("seo_score", Integer),
    # Core Web Vitals de laboratorio (ms salvo cls, que es un ratio sin unidad).
    Column("lcp_ms", Integer),
    Column("cls", Float),
    Column("tbt_ms", Integer),
    Column("fcp_ms", Integer),
    Column("si_ms", Integer),
    # Datos de campo (CrUX, usuarios reales) — solo si Google tiene tráfico
    # suficiente del sitio; None honesto si no hay (regla P1, nunca se
    # rellena con el dato de laboratorio disfrazado de dato de campo).
    Column("field_data_available", Boolean, default=False),
    Column("field_lcp_ms", Integer),
    Column("field_cls", Float),
    Column("field_inp_ms", Integer),
    Column("created_at", String, nullable=False),
    UniqueConstraint("project_id", "date", "strategy", "url", name="uq_pagespeed"),
)

indexation_status = Table(
    "indexation_status",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("url", Text, nullable=False),
    # Los siguientes son literalmente los valores que devuelve la URL
    # Inspection API de Search Console — nunca traducidos ni reinterpretados,
    # para no perder matices reales (regla P1). verdict es el resumen de 4
    # estados (PASS|FAIL|NEUTRAL|VERDICT_UNSPECIFIED); coverage_state es el
    # texto largo real de Google (ej. "Submitted and indexed",
    # "URL is unknown to Google").
    Column("verdict", String, nullable=False),
    Column("coverage_state", String),
    Column("robots_txt_state", String),
    Column("indexing_state", String),
    Column("page_fetch_state", String),
    Column("google_canonical", Text),
    Column("user_canonical", Text),
    Column("crawled_as", String),
    Column("last_google_crawl", String),  # None si Google nunca crawleó esta URL
    Column("checked_at", String, nullable=False),
    UniqueConstraint("project_id", "url", name="uq_indexation_status"),
)

serp_rankings = Table(
    "serp_rankings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("keyword", String, nullable=False),
    Column("date", String, nullable=False),
    # Posición real en el SERP de Google vía Serper — None si no aparece en
    # los resultados devueltos (regla P1: nunca se asume una posición peor
    # que "no visto", ni se inventa un top-N si no hay más páginas pedidas).
    Column("our_position", Integer),
    Column("our_url", Text),
    # {"dominio.com": posicion_int, ...} — solo dominios registrados como
    # competidores en el momento del escaneo; ausente del dict = no visible
    # en los resultados devueltos, no necesariamente "no rankea en absoluto".
    Column("competitor_positions", JSON, default=dict),
    # § mejoras 2026-07-25: captura OPORTUNISTA de peopleAlsoAsk /
    # relatedSearches / answerBox. Verificado en vivo el 2026-07-25 (7 queries):
    # Serper los devuelve con hl=en/gl=us pero NO con hl=es (probado co, es, mx),
    # así que para proyectos en español esto queda {} casi siempre — se guarda
    # igual porque viene en la MISMA respuesta ya pagada, sin request extra.
    Column("serp_features", JSON, default=dict),
    Column("checked_at", String, nullable=False),
    UniqueConstraint("project_id", "keyword", "date", name="uq_serp_rankings"),
)

serp_results = Table(
    "serp_results",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("keyword", String, nullable=False),
    Column("date", String, nullable=False),
    # El top-10 REAL que Google devolvió, no solo nuestra posición. Antes se
    # descartaba: cada llamada a Serper ya traía estos 10 resultados y solo se
    # leía la fila propia y la de competidores registrados a mano. Guardarlo
    # permite descubrir quién compite de verdad (no a quién creíamos) sin
    # gastar un solo crédito adicional.
    Column("position", Integer, nullable=False),  # absoluta: (page-1)*10 + position
    Column("url", Text, nullable=False),
    Column("domain", String, nullable=False),
    Column("title", Text),
    Column("snippet", Text),
    Column("is_ours", Boolean, default=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("project_id", "keyword", "date", "position", name="uq_serp_results"),
)

local_pack_rankings = Table(
    "local_pack_rankings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("keyword", String, nullable=False),
    Column("date", String, nullable=False),
    # Posición real en el Local Pack (Maps) de Google vía Serper /places — para
    # negocios físicos suele importar más que el ranking orgánico (serp_rankings)
    # porque las búsquedas de intención local ("cerca de mí") activan el pack
    # de Maps antes que el listado orgánico. None honesto si nuestro negocio no
    # aparece en las páginas consultadas (regla P1, mismo criterio que serp_rankings).
    Column("our_position", Integer),
    # Nombre exacto del listado en Maps (puede diferir del nombre "oficial" —
    # es lo que Google decidió mostrar, dato real, no se corrige).
    Column("our_listing_title", Text),
    Column("our_rating", Float),
    Column("our_reviews_count", Integer),
    Column("checked_at", String, nullable=False),
    UniqueConstraint("project_id", "keyword", "date", name="uq_local_pack_rankings"),
)

# AI Visibility (§ mejoras 2026-07-27): qué responden Gemini/Claude/DeepSeek
# EN VIVO cuando se les hace una pregunta real sobre el negocio o su categoría
# — no un promedio ni un tracking histórico de terceros, la respuesta real de
# la API en el momento de la corrida. Se guarda cada consulta como fila nueva
# (como serp_results/local_pack_rankings) en vez de sobreescribir: la misma
# pregunta puede responderse distinto la próxima vez, y eso también es dato.
ai_visibility_checks = Table(
    "ai_visibility_checks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
    Column("provider", String, nullable=False),  # gemini|claude|deepseek
    Column("prompt_type", String, nullable=False),  # brand|category
    Column("prompt", Text, nullable=False),
    Column("response_text", Text, nullable=False),
    # Detección honesta (P1): substring match del nombre/dominio del negocio
    # en la respuesta, sin acentos — NO es análisis de sentimiento ni de
    # relevancia, solo "¿aparece el negocio en el texto o no?". NULL para
    # prompts de marca (bug real detectado en vivo 2026-07-27: el prompt de
    # marca ya CONTIENE el nombre, así que la IA lo repite aunque diga "no lo
    # conozco" — el substring match ahí sería un falso positivo. Solo es una
    # señal real en prompts de categoría, donde el nombre no se dio antes.
    Column("mentions_business", Boolean),
    Column("checked_at", String, nullable=False),
)
