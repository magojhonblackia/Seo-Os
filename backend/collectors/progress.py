"""Progreso en vivo de un collector lento, en memoria (sin tocar la DB por
ítem). Genérico — lo usa el crawler (páginas) y la indexación (URLs
inspeccionadas contra Search Console); cualquier collector con un bucle
secuencial largo puede sumarse.

Por qué en memoria y no en SQLite: escribir a la DB en cada ítem sumaría
contención innecesaria. La app es single-process/single-worker local (uvicorn
workers=1, regla S8), así que un dict a nivel de módulo lo comparten el hilo
del collector (FastAPI corre los handlers `def` síncronos en un threadpool) y
el hilo que responde el GET de progreso — sin necesidad de IPC.

Se usa SOLO para UX (saber que el collector avanza y no se congeló). Bug real
que motivó generalizar esto (2026-07-24): la indexación (hasta 50 URLs contra
la URL Inspection API de Google, que es lenta — ~6-7s por llamada real)
tardó 6 minutos SIN ninguna señal de progreso, y la auditoría se veía
congelada en "Paso 6/10" — mismo síntoma que ya habíamos resuelto para el
crawler, pero nunca extendido a otros collectors lentos. Si el proceso se
reinicia, el progreso se pierde y el frontend deja de verlo — nunca es fuente
de verdad de ningún dato real (eso vive en la DB).
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_progress: dict[str, dict] = {}


def start(slug: str, total: int, phase: str = "running") -> None:
    with _lock:
        _progress[slug] = {
            "phase": phase,
            "pages_done": 0,
            "pages_total": total,
            "current_url": None,
            "started_at": time.time(),
            "updated_at": time.time(),
        }


def update(slug: str, done: int, current_url: str | None = None) -> None:
    with _lock:
        entry = _progress.get(slug)
        if entry is None:
            return
        entry["pages_done"] = done
        entry["current_url"] = current_url
        entry["updated_at"] = time.time()


def set_phase(slug: str, phase: str) -> None:
    with _lock:
        entry = _progress.get(slug)
        if entry is not None:
            entry["phase"] = phase
            entry["updated_at"] = time.time()


def finish(slug: str, *, status: str, summary: dict | None = None, message: str | None = None) -> None:
    """Marca el trabajo como terminado y DEJA el resultado disponible para que
    el frontend lo recoja en su siguiente sondeo (§ bug real 2026-07-25).

    Por qué el resultado vive aquí y no solo en la respuesta HTTP: la
    indexación tarda ~6 minutos y mantener el POST abierto todo ese tiempo sin
    enviar un byte hacía que el navegador cortara la conexión ("Failed to
    fetch") aunque el collector terminara bien — el trabajo se completaba y el
    usuario veía un fallo. Ahora la petición vuelve de inmediato y el
    resultado se entrega por esta vía. Sigue sin ser fuente de verdad: el dato
    real está en `snapshots` y en las tablas (S2).
    """
    with _lock:
        entry = _progress.get(slug)
        if entry is None:
            entry = {
                "phase": "finished",
                "pages_done": 0,
                "pages_total": 0,
                "current_url": None,
                "started_at": time.time(),
            }
            _progress[slug] = entry
        entry["finished"] = True
        entry["updated_at"] = time.time()
        entry["result"] = {"status": status, "summary": summary, "message": message}


def is_running(slug: str) -> bool:
    """True si hay un trabajo en curso (lanzado y aún sin terminar) — evita
    disparar dos veces el mismo collector pesado por doble clic."""
    with _lock:
        entry = _progress.get(slug)
        return entry is not None and not entry.get("finished", False)


def clear(slug: str) -> None:
    with _lock:
        _progress.pop(slug, None)


def get(slug: str) -> dict | None:
    with _lock:
        entry = _progress.get(slug)
        if entry is None:
            return None
        snapshot = dict(entry)
    snapshot["elapsed_seconds"] = round(time.time() - snapshot["started_at"], 1)
    snapshot.setdefault("finished", False)
    return snapshot
