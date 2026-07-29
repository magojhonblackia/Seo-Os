"""Dependencias compartidas de la API. Regla §4.3: todo `site`/`project` en un
request se resuelve contra la tabla `projects` ANTES de usarse en cualquier otra
cosa (crawler, queries). Nunca se acepta una URL cruda del cliente."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from backend.db.database import get_connection
from backend.db.schema import projects


def get_project_or_404(slug: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(select(projects).where(projects.c.slug == slug)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Proyecto '{slug}' no existe")
    return dict(row._mapping)
