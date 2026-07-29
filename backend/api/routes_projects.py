"""CRUD de proyectos. Registrar un proyecto nuevo (POST) es lo que habilita
auditar un sitio propio distinto de los 4 sembrados en Fase 0 — sin esto,
la plataforma solo servía para los sitios ya conocidos del usuario.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import insert, select, update

from backend.analyzers.url_safety import UnsafeURLError, validate_public_url
from backend.api.deps import get_project_or_404
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.models.schemas import AddCompetitorRequest, ProjectCreate, ProjectOut

MAX_COMPETITORS = 10

router = APIRouter(prefix="/api/projects", tags=["projects"])

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


@router.get("", response_model=list[ProjectOut])
def list_projects() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(select(projects).where(projects.c.is_active.is_(True))).all()
    return [dict(r._mapping) for r in rows]


@router.get("/{slug}", response_model=ProjectOut)
def get_project(project: dict = Depends(get_project_or_404)) -> dict:
    return project


def _slugify_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    base = _SLUG_INVALID_CHARS.sub("-", domain).strip("-")
    return base or "sitio"


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate) -> dict:
    domain = urlparse(payload.url).netloc.lower().removeprefix("www.")
    base_slug = _slugify_domain(payload.url)

    with get_connection() as conn:
        existing_slugs = {row[0] for row in conn.execute(select(projects.c.slug))}

        slug = base_slug
        suffix = 2
        while slug in existing_slugs:
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        now = now_iso()
        project_id = conn.execute(
            insert(projects).values(
                slug=slug,
                name=payload.name,
                url=payload.url,
                gsc_property=f"sc-domain:{domain}",
                country=payload.country.upper(),
                language=payload.language.lower(),
                competitors=payload.competitors,
                is_active=True,
                config={},
                created_at=now,
            )
        ).inserted_primary_key[0]

        row = conn.execute(select(projects).where(projects.c.id == project_id)).first()

    return dict(row._mapping)


@router.post("/{slug}/competitors", response_model=ProjectOut)
def add_competitor(payload: AddCompetitorRequest, project: dict = Depends(get_project_or_404)) -> dict:
    """Agrega un dominio a `projects.competitors` a partir de una URL pegada
    por el usuario — antes solo se podían fijar competidores al crear el
    proyecto. Pasa por el mismo guard SSRF que el análisis rápido de URL
    (regla §4.3: nunca aceptar una URL de usuario sin validar) antes de
    guardarla; el escaneo real ocurre después, vía POST /api/collect/competitor."""
    try:
        safe_url = validate_public_url(payload.url)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    domain = urlparse(safe_url).netloc.lower().removeprefix("www.")
    own_domain = urlparse(project["url"]).netloc.lower().removeprefix("www.")
    if domain == own_domain:
        raise HTTPException(status_code=400, detail="No puedes agregar tu propio dominio como competidor")

    current = list(project["competitors"] or [])
    if domain in current:
        raise HTTPException(status_code=400, detail=f"'{domain}' ya está registrado como competidor")
    if len(current) >= MAX_COMPETITORS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_COMPETITORS} competidores por proyecto")

    current.append(domain)
    with get_connection() as conn:
        conn.execute(update(projects).where(projects.c.id == project["id"]).values(competitors=current))
        row = conn.execute(select(projects).where(projects.c.id == project["id"])).first()

    return dict(row._mapping)


@router.delete("/{slug}", status_code=204, response_model=None)
def delete_project(project: dict = Depends(get_project_or_404)):
    """Baja lógica (is_active=False), no borra filas. Regla P4: nunca se destruye
    data sin poder recuperarla — snapshots/issues/keywords del proyecto quedan
    intactos en la base por si se necesita reactivar o auditar después."""
    with get_connection() as conn:
        conn.execute(update(projects).where(projects.c.id == project["id"]).values(is_active=False))
