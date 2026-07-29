"""Tests de collectors: parseo con HTML grabado, SIN llamadas a internet (regla QA)."""
from pathlib import Path

from sqlalchemy import insert, select

from backend.analyzers.issue_store import reconcile_page_issues, reconcile_project_issues
from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import _extract_page_data, _normalize_url
from backend.db.database import get_connection, now_iso
from backend.db.schema import issues, pages, projects, snapshots

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class _ExplodingCollector(BaseCollector):
    """Collector de prueba que siempre lanza, para validar la regla S3:
    un fallo de collect() nunca debe escapar de run() ni tumbar la app."""

    name = "exploding_test_collector"

    def collect(self) -> CollectorResult:
        raise RuntimeError("fuente externa caída, esto NO debe tumbar la app")


def test_collector_error_no_escapa_y_queda_registrado_como_error():
    collector = _ExplodingCollector("jc")
    snapshot_id = collector.run()  # no debe lanzar, pase lo que pase en collect()

    with get_connection() as conn:
        row = conn.execute(select(snapshots).where(snapshots.c.id == snapshot_id)).first()

    assert row is not None
    assert row.status == "error"
    assert "fuente externa caída" in row.error_message


def test_extract_page_data_pagina_completa():
    html = _load("sample_page.html")
    page = _extract_page_data("https://jcreparaciones.com/reparacion-iphone-cali", 200, html)

    assert page.title == "Reparación iPhone en Cali | JC Reparaciones"
    assert page.meta_description and page.meta_description.startswith("Reparación de iPhone en Cali")
    assert page.h1_tags == ["Reparación de iPhone en Cali"]
    assert "LocalBusiness" in page.schema_types
    assert not page.schema_has_errors
    assert page.og == {
        "title": "Reparación iPhone en Cali",
        "description": "Servicio técnico especializado",
        "image": "https://jcreparaciones.com/og.jpg",
    }
    assert page.canonical == "https://jcreparaciones.com/reparacion-iphone-cali"
    assert page.is_indexable is True
    assert page.lang_declared == "es"


def test_extract_page_data_solo_enlaces_internos():
    html = _load("sample_page.html")
    page = _extract_page_data("https://jcreparaciones.com/reparacion-iphone-cali", 200, html)

    internal = [link for link in page.internal_links if "jcreparaciones.com" in link]
    external = [link for link in page.internal_links if "competidor-externo.com" in link]
    assert len(internal) == 2
    assert external == []  # el enlace externo no debe colarse como interno


def test_extract_page_data_pagina_zombie():
    html = _load("zombie_page.html")
    page = _extract_page_data("https://jcreparaciones.com/volantes-cali", 200, html)

    assert page.h1_tags == []
    assert page.is_indexable is False
    assert not page.title


def test_extract_page_data_br_en_h1_se_convierte_en_espacio():
    # § #2: el fixture es '<h1>REPARACIÓN<br/>iPHONE en Cali</h1>'. Un <br/> es
    # un salto de línea LEGÍTIMO, no un bug — al aplanar debe quedar un espacio.
    # Antes se extraía 'REPARACIÓNiPHONE en Cali' (texto basura que además
    # ensuciaba word_count/keyword/contexto IA). Ahora las palabras quedan
    # separadas por el espacio que el <br/> representa.
    html = _load("stuck_words_page.html")
    page = _extract_page_data("https://jcreparaciones.com/reparacion-iphone-cali", 200, html)

    assert page.h1_tags == ["REPARACIÓN iPHONE en Cali"]  # con espacio, no "REPARACIÓNiPHONE"


def test_extract_page_data_h1_caso_real_jc_todo_mayusculas_limpio():
    # Caso real verificado en vivo: el H1 de jcreparaciones.com es
    # 'REPARACIÓN DE<br/>CELULARES<br/>EN CALI' repartido en spans. Con el fix
    # queda texto limpio y NO se marca como palabras pegadas (falso positivo
    # que antes ensuciaba el reporte).
    from backend.analyzers.technical import detect_stuck_words
    html = (
        "<html lang='es'><body><h1>REPARACIÓN DE<!-- --> <br/>"
        "<span>CELULARES</span> <br/><span>EN CALI</span></h1></body></html>"
    )
    page = _extract_page_data("https://jcreparaciones.com/", 200, html)
    assert page.h1_tags == ["REPARACIÓN DE CELULARES EN CALI"]
    assert detect_stuck_words(page.h1_tags[0]) is False


def test_extract_page_data_concatenacion_real_sin_tag_si_se_detecta():
    # El bug REAL de palabras pegadas es una concatenación SIN ningún tag ni
    # espacio (nodo de texto único). Eso sí se sigue detectando.
    from backend.analyzers.technical import detect_stuck_words
    html = "<html lang='es'><body><h1>ServicioTecnicoBarato</h1></body></html>"
    page = _extract_page_data("https://x.com/", 200, html)
    assert page.h1_tags == ["ServicioTecnicoBarato"]
    assert detect_stuck_words(page.h1_tags[0]) is True


# ---------- Reconciliación de issues (§ falsos positivos que quedaban 'open') ----------
def _mk_project_and_page(slug: str):
    with get_connection() as conn:
        pid = conn.execute(
            insert(projects).values(
                slug=slug, name="Recon", url="https://recon.com",
                gsc_property="sc-domain:recon.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        page_id = conn.execute(
            insert(pages).values(project_id=pid, url="https://recon.com/x", first_seen=now_iso())
        ).inserted_primary_key[0]
    return pid, page_id


def test_reconcile_cierra_issue_del_crawler_que_ya_no_se_reproduce():
    pid, page_id = _mk_project_and_page("recon-cierra")
    with get_connection() as conn:
        # issue vieja del crawler (page_id no-nulo, categoría 'h1') que el análisis
        # fresco ya NO reporta -> debe cerrarse como 'resolved'.
        stale_id = conn.execute(
            insert(issues).values(
                project_id=pid, page_id=page_id, severity="high", category="h1",
                title="https://recon.com/x: H1 con palabras pegadas", effort="1h",
                impact=4, status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]

        closed = reconcile_page_issues(conn, project_id=pid, page_id=page_id, fresh_keys=set(), now=now_iso())
        assert closed == 1

        row = conn.execute(select(issues.c.status, issues.c.resolved_at).where(issues.c.id == stale_id)).first()
        assert row.status == "resolved"
        assert row.resolved_at is not None


def test_reconcile_no_cierra_lo_que_sigue_vigente_ni_lo_de_otros_analyzers():
    pid, page_id = _mk_project_and_page("recon-preserva")
    with get_connection() as conn:
        vigente = conn.execute(
            insert(issues).values(
                project_id=pid, page_id=page_id, severity="high", category="meta",
                title="https://recon.com/x: Meta sin CTA", effort="5min", impact=5,
                status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]
        # canibalización = page_id None, categoría fuera del set del crawler -> intocable
        canib = conn.execute(
            insert(issues).values(
                project_id=pid, page_id=None, severity="critical", category="cannibalization",
                title="'x': 2 páginas compitiendo", effort="1d", impact=4,
                status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]

        closed = reconcile_page_issues(
            conn, project_id=pid, page_id=page_id,
            fresh_keys={("meta", "https://recon.com/x: Meta sin CTA")}, now=now_iso(),
        )
        assert closed == 0  # el vigente sigue en el set fresco; el de canib no es del crawler

        statuses = {
            r.id: r.status
            for r in conn.execute(select(issues.c.id, issues.c.status).where(issues.c.id.in_([vigente, canib]))).all()
        }
        assert statuses[vigente] == "open"
        assert statuses[canib] == "open"


def test_reconcile_project_cierra_pruning_viejo_solo_page_id_null():
    pid, page_id = _mk_project_and_page("recon-proyecto")
    with get_connection() as conn:
        # pruning viejo (page_id None) con título anterior -> ya no aparece -> cerrar
        viejo = conn.execute(
            insert(issues).values(
                project_id=pid, page_id=None, severity="medium", category="pruning",
                title="https://recon.com/x: sin impresiones en los datos GSC cargados",
                effort="1h", impact=2, status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]
        # un issue del crawler (page_id no-nulo, misma categoría 'meta') NO debe tocarse
        crawler_meta = conn.execute(
            insert(issues).values(
                project_id=pid, page_id=page_id, severity="high", category="meta",
                title="https://recon.com/x: Meta sin CTA", effort="5min", impact=5,
                status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]

        closed = reconcile_project_issues(
            conn, project_id=pid,
            owned_categories={"cannibalization", "pruning", "decay", "meta"},
            fresh_keys=set(), now=now_iso(),
        )
        assert closed == 1  # solo el pruning page_id-null

        statuses = {
            r.id: r.status
            for r in conn.execute(select(issues.c.id, issues.c.status).where(issues.c.id.in_([viejo, crawler_meta]))).all()
        }
        assert statuses[viejo] == "resolved"
        assert statuses[crawler_meta] == "open"  # del crawler, intocable por el scope page_id IS NULL


# ---------- Progreso en vivo de collectors lentos (§ barra de progreso) ----------
def test_progress_store_reporta_avance_y_se_limpia():
    """Genérico (2026-07-24): lo usa el crawler (fase 'crawling') Y la
    indexación (fase 'checking_indexation', ver test_indexation.py) — mismo
    store, distinta fase."""
    from backend.collectors import progress

    progress.clear("test-prog")
    assert progress.get("test-prog") is None

    progress.start("test-prog", total=50, phase="crawling")
    progress.update("test-prog", 3, "https://x.com/a")
    state = progress.get("test-prog")
    assert state["phase"] == "crawling"
    assert state["pages_done"] == 3
    assert state["pages_total"] == 50
    assert state["current_url"] == "https://x.com/a"
    assert "elapsed_seconds" in state

    progress.set_phase("test-prog", "analyzing")
    assert progress.get("test-prog")["phase"] == "analyzing"


# ---------- Ejecución en segundo plano (§ bug real 2026-07-25) ----------

def test_finish_deja_el_resultado_disponible_para_el_frontend():
    """El resultado ya no viaja en la respuesta del POST (que moría por corte
    de conexión a los ~6 min): se deposita aquí y el frontend lo recoge."""
    from backend.collectors import progress

    progress.clear("test-bg")
    progress.start("test-bg", total=50, phase="checking_indexation")
    assert progress.is_running("test-bg") is True

    progress.finish("test-bg", status="partial", summary={"urls_checked": 49})
    state = progress.get("test-bg")
    assert state["finished"] is True
    assert state["result"]["status"] == "partial"
    assert state["result"]["summary"] == {"urls_checked": 49}
    assert progress.is_running("test-bg") is False
    progress.clear("test-bg")


def test_finish_funciona_aunque_el_collector_ya_haya_limpiado_su_progreso():
    """indexation.py hace progress.clear() en su finally, así que finish()
    puede llegar sin entrada previa — no debe perder el resultado."""
    from backend.collectors import progress

    progress.clear("test-bg-sin-entrada")
    progress.finish("test-bg-sin-entrada", status="ok", summary={"x": 1})
    state = progress.get("test-bg-sin-entrada")
    assert state["finished"] is True
    assert state["result"]["summary"] == {"x": 1}
    progress.clear("test-bg-sin-entrada")


def test_get_sin_entrada_sigue_devolviendo_none():
    from backend.collectors import progress

    progress.clear("test-bg-inexistente")
    assert progress.get("test-bg-inexistente") is None

    progress.clear("test-prog")
    assert progress.get("test-prog") is None


def test_api_progress_endpoint_sin_crawl_activo():
    from fastapi.testclient import TestClient

    from backend.main import app

    resp = TestClient(app).get("/api/collect/progress/proyecto-sin-crawl-activo")
    assert resp.status_code == 200
    assert resp.json() == {"active": False}


def test_normalize_url_quita_slash_final():
    assert _normalize_url("https://jcreparaciones.com/") == "https://jcreparaciones.com/"
    assert _normalize_url("https://jcreparaciones.com/pagina/") == "https://jcreparaciones.com/pagina"


def test_normalize_url_quita_fragmento():
    assert _normalize_url("https://x.com/pagina#seccion") == "https://x.com/pagina"


def test_normalize_url_idempotente():
    url = "https://jcreparaciones.com/reparacion-iphone-cali"
    assert _normalize_url(url) == _normalize_url(_normalize_url(url))


# ---------- Señales E-E-A-T (§9 Fase 1) ----------
def test_extract_page_data_detecta_señales_eeat_completas():
    html = _load("eeat_page.html")
    page = _extract_page_data("https://x.com/guia", 200, html)
    assert page.has_author is True
    assert page.has_date is True
    assert page.has_contact is True


def test_extract_page_data_sin_señales_eeat():
    html = _load("no_eeat_page.html")
    page = _extract_page_data("https://x.com/generica", 200, html)
    assert page.has_author is False
    assert page.has_date is False
    assert page.has_contact is False


def test_extract_page_data_incluye_body_text_sample():
    html = _load("sample_page.html")
    page = _extract_page_data("https://jcreparaciones.com/reparacion-iphone-cali", 200, html)
    assert "servicio técnico especializado" in page.body_text_sample.lower()
