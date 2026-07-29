"""Aísla los tests de la base de datos real (regla QA: nunca contaminar data/seo.db).

Debe ejecutarse ANTES de que cualquier módulo importe backend.config (que
instancia `settings` al importarse), así que se fija la variable de entorno
a nivel de módulo, no dentro de un fixture — pytest importa conftest.py antes
de recolectar los módulos de test.
"""
import os
import tempfile
from pathlib import Path

import pytest

_tmp_dir = tempfile.mkdtemp(prefix="seo_os_test_")
os.environ["DATABASE_PATH"] = str(Path(_tmp_dir) / "test_seo.db")


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database():
    """Crea el schema + seed en la DB temporal antes de cualquier test.

    Necesario para módulos de test que no importan backend.main (que es lo
    único que dispara run_migrations() como efecto secundario del import).
    """
    from backend.db.migrations import run_migrations

    run_migrations()
