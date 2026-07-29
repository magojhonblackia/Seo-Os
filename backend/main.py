"""Punto de entrada FastAPI. Bind solo 127.0.0.1 (regla S8), CORS restringido (§4.2)."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api import (
    routes_ai,
    routes_alerts,
    routes_collectors,
    routes_dashboard,
    routes_projects,
    routes_quick_analysis,
    routes_scheduler,
    routes_settings,
)
from backend.api.auth import require_auth_if_configured
from backend.config import configure_logging, settings
from backend.db.migrations import run_migrations

configure_logging()
run_migrations()

app = FastAPI(title="SEO Operating System", version="0.1.0")

_allowed_origins = [
    f"http://127.0.0.1:{settings.port}",
    f"http://localhost:{settings.port}",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_store_api_responses(request: Request, call_next):
    """Los datos de la API son dinámicos: nunca deben cachearse en el navegador.

    Bug real (2026-07-23): las respuestas JSON de /api/dashboard/* salían SIN
    Cache-Control, así que el navegador les aplicaba caché heurística (RFC 7234)
    y, tras re-ejecutar una auditoría, la SPA volvía a pedir p.ej.
    /api/dashboard/jc/technical y recibía el JSON VIEJO de su caché de disco sin
    revalidar — el usuario veía "el mismo reporte viejo" aunque el backend ya
    tenía datos frescos. Marcar no-store en todo /api/* mata esa clase de bug.
    (Los estáticos siguen con no-cache+ETag: revalidan pero pueden reusar 304.)
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

# Auth opcional por Bearer token (§4.2, S8): sin AUTH_TOKEN configurado, este
# dependency es un no-op — solo se activa si el usuario decidió exponer la
# app fuera de 127.0.0.1 (config.py ya exige el token en ese caso al arrancar).
_auth_dep = [Depends(require_auth_if_configured)]
app.include_router(routes_projects.router, dependencies=_auth_dep)
app.include_router(routes_dashboard.router, dependencies=_auth_dep)
app.include_router(routes_collectors.router, dependencies=_auth_dep)
app.include_router(routes_ai.router, dependencies=_auth_dep)
app.include_router(routes_scheduler.router, dependencies=_auth_dep)
app.include_router(routes_quick_analysis.router, dependencies=_auth_dep)
app.include_router(routes_alerts.router, dependencies=_auth_dep)
app.include_router(routes_settings.router, dependencies=_auth_dep)

class _NoCacheStaticFiles(StaticFiles):
    """Sin Cache-Control, el navegador aplica heurística de frescura sobre
    Last-Modified (RFC 7234) y puede servir JS/CSS editados como si siguieran
    vigentes por horas — bug real encontrado 2026-07-15 verificando el panel
    de Core Web Vitals en el navegador: una pestaña abierta seguía ejecutando
    `scorecard.js` de antes de una edición, sin ningún error visible. Forzar
    revalidación (no-cache ≠ no-store: el navegador igual puede usar el ETag
    y recibir 304) es gratis en un servidor local y evita que "recargar la
    página" mienta sobre si el código cambió."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", _NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
