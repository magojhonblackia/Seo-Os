"""Carga datos reales de GSC exportados desde el MCP SEO Gets (§5.1 del PROMPT_MAESTRO).

La conexión GSC vía MCP vive en la sesión de Claude, no en el Python del usuario.
Este script toma el JSON que Claude genera con esos datos reales y los inserta
en SQLite de forma idempotente (upsert por las claves únicas de §3), para que
el dashboard nazca con datos verdaderos en vez de placeholders.

Uso:
    python -m scripts.bootstrap_data scripts/gsc_bootstrap_jc.json
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.config import configure_logging
from backend.db.database import get_connection, now_iso
from backend.db.migrations import run_migrations
from backend.db.schema import gsc_daily, gsc_queries, projects

logger = logging.getLogger(__name__)


def _project_id(conn, slug: str) -> int:
    row = conn.execute(select(projects.c.id).where(projects.c.slug == slug)).first()
    if row is None:
        raise ValueError(f"Proyecto '{slug}' no existe. Corre las migraciones primero (incluyen el seed).")
    return row[0]


def load_gsc_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def bootstrap_from_file(path: Path) -> dict:
    data = load_gsc_json(path)
    slug = data["project_slug"]
    now = now_iso()

    with get_connection() as conn:
        pid = _project_id(conn, slug)

        daily_rows = 0
        for d in data.get("daily", []):
            impressions = d["impressions"]
            ctr = (d["clicks"] / impressions) if impressions else 0.0
            stmt = sqlite_insert(gsc_daily).values(
                project_id=pid,
                date=d["date"],
                clicks=d["clicks"],
                impressions=impressions,
                ctr=ctr,
                position=d["position"],
                created_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["project_id", "date"],
                set_={"clicks": d["clicks"], "impressions": impressions, "ctr": ctr, "position": d["position"]},
            )
            conn.execute(stmt)
            daily_rows += 1

        query_rows = 0
        query_date = data.get("end_date", now[:10])
        for q in data.get("queries", []):
            impressions = q["impressions"]
            ctr = (q["clicks"] / impressions) if impressions else 0.0
            stmt = sqlite_insert(gsc_queries).values(
                project_id=pid,
                date=query_date,
                query=q["query"],
                page=q.get("page"),
                clicks=q["clicks"],
                impressions=impressions,
                ctr=ctr,
                position=q["position"],
                created_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["project_id", "date", "query", "page"],
                set_={"clicks": q["clicks"], "impressions": impressions, "ctr": ctr, "position": q["position"]},
            )
            conn.execute(stmt)
            query_rows += 1

    logger.info("Bootstrap %s: %d filas diarias, %d queries", slug, daily_rows, query_rows)
    return {"project": slug, "daily_rows": daily_rows, "query_rows": query_rows}


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Bootstrap de datos GSC reales exportados vía MCP")
    parser.add_argument("json_path", help="Ruta al JSON con datos GSC (ver formato en scripts/gsc_bootstrap_jc.json)")
    args = parser.parse_args()

    run_migrations()
    result = bootstrap_from_file(Path(args.json_path))
    print(json.dumps(result, indent=2, ensure_ascii=False))
