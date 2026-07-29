"""Clase base para collectors (reglas S2, S3, S7 del PROMPT_MAESTRO).

Contrato de todo collector:
1. Recolecta datos crudos.
2. Los guarda en `snapshots` ANTES de procesarlos (S2) — si el análisis
   posterior tiene un bug, los datos crudos sobreviven.
3. Nunca lanza una excepción sin capturar hacia afuera: un fallo de red se
   traduce en snapshot status="error" para que el resto de la app se
   degrade con gracia (S3), nunca en una pantalla rota.
4. Es ejecutable de forma aislada (S7): `python -m backend.collectors.<modulo>`.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select

from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    status: str  # ok|error|partial
    raw_data: Any
    error_message: str | None = None


class BaseCollector(ABC):
    name: str = "base"

    def __init__(self, project_slug: str):
        self.project_slug = project_slug
        self.project = self._load_project()

    def _load_project(self) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                select(projects).where(projects.c.slug == self.project_slug)
            ).first()
        if row is None:
            raise ValueError(
                f"Proyecto '{self.project_slug}' no existe en la tabla projects. "
                "Usa un slug ya registrado (ver seed en backend/db/migrations.py)."
            )
        return dict(row._mapping)

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Ejecuta la recolección real. Debe capturar sus propios errores de red
        (timeouts, DNS, HTTP 4xx/5xx) y devolver CollectorResult(status="error"|
        "partial", ...) en vez de dejar escapar la excepción.
        """
        raise NotImplementedError

    def run(self) -> int:
        """Ejecuta collect(), guarda el snapshot crudo y devuelve su id."""
        started = now_iso()
        try:
            result = self.collect()
        except Exception as exc:  # noqa: BLE001 - último resorte, nunca tumba la app
            logger.exception("Collector %s falló de forma inesperada", self.name)
            result = CollectorResult(status="error", raw_data=None, error_message=str(exc))

        finished = now_iso()
        with get_connection() as conn:
            inserted = conn.execute(
                insert(snapshots).values(
                    project_id=self.project["id"],
                    collector=self.name,
                    status=result.status,
                    started_at=started,
                    finished_at=finished,
                    error_message=result.error_message,
                    raw_data=result.raw_data,
                    created_at=now_iso(),
                )
            )
            snapshot_id = inserted.inserted_primary_key[0]

        if result.status == "error":
            logger.warning(
                "Collector %s para %s terminó con error: %s",
                self.name, self.project_slug, result.error_message,
            )
        else:
            logger.info(
                "Collector %s para %s terminó: %s", self.name, self.project_slug, result.status
            )
        return snapshot_id
