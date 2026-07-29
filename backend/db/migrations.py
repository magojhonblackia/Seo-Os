"""Migraciones secuenciales idempotentes (regla S5). Nunca DROP sin aprobación."""
from __future__ import annotations

import logging

from sqlalchemy import insert, select, text

from backend.db.database import get_connection, init_db, now_iso
from backend.db.schema import pagespeed, projects

logger = logging.getLogger(__name__)

# Seed de los 4 proyectos reales del usuario (§5.2 del PROMPT_MAESTRO).
SEED_PROJECTS = [
    {
        "slug": "jc",
        "name": "JC Reparaciones",
        "url": "https://jcreparaciones.com",
        "gsc_property": "sc-domain:jcreparaciones.com",
        "country": "CO",
        "language": "es",
        "competitors": [
            "capriservicios.com",
            "serviciotecnicoapplecali.com",
            "myphonedoctor.co",
        ],
    },
    {
        "slug": "komaromi",
        "name": "Komaromi Print Service",
        "url": "https://komaromiprintservice.com",
        "gsc_property": "sc-domain:komaromiprintservice.com",
        "country": "CO",
        "language": "es",
        "competitors": [
            "marketingpublicidadcali.com",
            "publiknet.net",
            "iconimpresiones.com",
        ],
    },
    {
        "slug": "fixio",
        "name": "SoyFixio",
        "url": "https://soyfixio.com",
        "gsc_property": "sc-domain:soyfixio.com",
        "country": "CO",
        "language": "es",
        "competitors": [],
    },
    {
        "slug": "fixio-tech",
        "name": "SoyFixio Tech",
        "url": "https://tech.soyfixio.com",
        "gsc_property": "sc-domain:tech.soyfixio.com",
        "country": "CO",
        "language": "es",
        "competitors": [],
    },
]


# Columnas agregadas a `pages` después del schema original de Fase 0 (regla S5:
# migraciones aditivas). metadata.create_all() NO altera tablas ya existentes,
# así que las bases de datos creadas en Fase 0 necesitan este ALTER explícito.
_PAGES_COLUMNS_FASE1 = {
    "readability_score": "INTEGER",
    "eeat_score": "INTEGER",
    "has_author": "BOOLEAN",
    "has_date": "BOOLEAN",
    "has_contact": "BOOLEAN",
    "x_robots_tag": "TEXT",  # § herramientas de mercado 2026-07-24
    "redirected_to": "TEXT",  # § bug real 2026-07-24: acumular cobertura de redirects entre crawls
}


# Columnas agregadas a otras tablas después de su creación original. Mismo
# criterio que _PAGES_COLUMNS_FASE1: ALTER TABLE aditivo e idempotente (S5).
_EXTRA_COLUMNS_BY_TABLE = {
    "serp_rankings": {
        "serp_features": "TEXT",  # JSON; § mejoras 2026-07-25, captura oportunista de PAA/answerBox
    },
}


def _add_missing_columns(conn) -> None:
    existing = {row[1] for row in conn.execute(text("PRAGMA table_info(pages)"))}
    for column, col_type in _PAGES_COLUMNS_FASE1.items():
        if column not in existing:
            conn.execute(text(f"ALTER TABLE pages ADD COLUMN {column} {col_type}"))
            logger.info("Migración: agregada columna pages.%s", column)

    for table_name, columns in _EXTRA_COLUMNS_BY_TABLE.items():
        table_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table_name}
        ).first()
        if not table_exists:
            continue  # init_db() ya la crea con las columnas nuevas incluidas
        present = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))}
        for column, col_type in columns.items():
            if column not in present:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {col_type}"))
                logger.info("Migración: agregada columna %s.%s", table_name, column)


def _migrate_pagespeed_add_url(conn) -> None:
    """La tabla `pagespeed` originalmente medía SOLO la home (1 fila por
    día+estrategia, § herramientas de mercado 2026-07-24: ahora es 1 fila por
    URL+día+estrategia). SQLite no soporta ALTER de UNIQUE constraints, así que
    se recrea la tabla — preservando cada fila vieja con `url` = la home del
    proyecto (que es exactamente lo que esas filas medían, regla P4: no se
    destruye nada, se completa el dato que faltaba)."""
    table_exists = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='pagespeed'")
    ).first()
    if not table_exists:
        return  # no existe aún; init_db() ya la crea con 'url' incluido

    existing_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(pagespeed)"))}
    if "url" in existing_columns:
        return  # ya migrada

    logger.info("Migración: recreando tabla pagespeed con columna 'url' (antes solo medía la home)")
    conn.execute(text("ALTER TABLE pagespeed RENAME TO pagespeed_old"))
    pagespeed.create(conn)
    conn.execute(
        text(
            """
            INSERT INTO pagespeed (
                project_id, date, strategy, url, performance_score, accessibility_score,
                best_practices_score, seo_score, lcp_ms, cls, tbt_ms, fcp_ms, si_ms,
                field_data_available, field_lcp_ms, field_cls, field_inp_ms, created_at
            )
            SELECT o.project_id, o.date, o.strategy, p.url, o.performance_score,
                o.accessibility_score, o.best_practices_score, o.seo_score, o.lcp_ms,
                o.cls, o.tbt_ms, o.fcp_ms, o.si_ms, o.field_data_available, o.field_lcp_ms,
                o.field_cls, o.field_inp_ms, o.created_at
            FROM pagespeed_old o
            JOIN projects p ON p.id = o.project_id
            """
        )
    )
    conn.execute(text("DROP TABLE pagespeed_old"))


def run_migrations() -> None:
    """Crea el schema (aditivo), agrega columnas nuevas a tablas existentes y
    siembra los proyectos si no existen aún."""
    init_db()
    with get_connection() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        _add_missing_columns(conn)
        _migrate_pagespeed_add_url(conn)
        _seed_projects(conn)


def _seed_projects(conn) -> None:
    existing_slugs = {row[0] for row in conn.execute(select(projects.c.slug))}
    to_insert = [
        {
            "slug": p["slug"],
            "name": p["name"],
            "url": p["url"],
            "gsc_property": p["gsc_property"],
            "country": p["country"],
            "language": p["language"],
            "competitors": p["competitors"],
            "is_active": True,
            "config": {},
            "created_at": now_iso(),
        }
        for p in SEED_PROJECTS
        if p["slug"] not in existing_slugs
    ]
    if to_insert:
        conn.execute(insert(projects), to_insert)
        logger.info("Seed: insertados %d proyectos nuevos", len(to_insert))
    else:
        logger.info("Seed: los proyectos ya existían, nada que insertar")


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    run_migrations()
    print("Migraciones aplicadas y proyectos sembrados.")
