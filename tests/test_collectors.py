"""Tests de collectors: parseo con HTML grabado, SIN llamadas a internet (regla QA)."""
from pathlib import Path

from bs4 import BeautifulSoup
from sqlalchemy import insert, select

from backend.analyzers.issue_store import reconcile_page_issues, reconcile_project_issues
from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import (
    _extract_page_data,
    _looks_like_question_heading,
    _normalize_url,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import issues, pages, projects, snapshots

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


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


# ---------- Extraíbilidad de respuestas por IA (§ mejoras 2026-08-02) ----------

def test_looks_like_question_heading_reconoce_preguntas_reales_en_español():
    assert _looks_like_question_heading("¿Cuánto cuesta cambiar la pantalla?")
    assert _looks_like_question_heading("¿Hacen envíos a otras ciudades?")
    assert _looks_like_question_heading("Cuánto dura la batería de un iPhone")  # sin ¿, empieza con interrogativo


def test_looks_like_question_heading_excluye_titulos_de_guia_sin_signo_de_pregunta():
    """'Cómo + infinitivo' sin '?' es un título de guía instructiva ('Cómo
    cambiar la batería'), no una pregunta que necesite una respuesta corta."""
    assert not _looks_like_question_heading("Cómo cuidar tu celular para que dure más")
    assert not _looks_like_question_heading("Cómo revisar el estado de la batería en tu iPhone")
    assert not _looks_like_question_heading("Servicios")
    assert not _looks_like_question_heading("")


def test_looks_like_question_heading_el_signo_de_pregunta_le_gana_al_titulo_de_guia():
    """Bug real encontrado verificando en vivo contra jcreparaciones.com:
    '¿Cómo saber si la batería está degradada?' es una pregunta real (termina
    en '?') pero 'saber' también es infinitivo — la exclusión de título-guía
    la descartaba antes de mirar el signo de pregunta. El '?' final debe
    ganarle siempre a esa heurística, nunca al revés."""
    assert _looks_like_question_heading("¿Cómo saber si la batería del iPhone está degradada?")


def test_analyze_answer_extractability_caso_real():
    html = _load("answer_extractability_page.html")
    page = _extract_page_data("https://x.com/preguntas", 200, html)
    ae = page.answer_extractability

    # 3 encabezados de pregunta real: cambio de pantalla, envíos, por qué elegirnos.
    # "Cómo cuidar tu celular..." y "Servicios" correctamente excluidos.
    assert ae["question_headings_count"] == 3
    # "¿Hacen envíos...?" va seguida de una <img>, no de un <p>/<li> con texto —
    # sin respuesta directa que una IA pueda citar.
    assert ae["question_headings_without_answer"] == 1
    assert ae["long_passages_over_250w"] == 0
    assert ae["has_tldr_or_summary"] is False


def test_build_answer_extractability_issues_solo_reporta_lo_que_tiene_evidencia_clara():
    from backend.analyzers.content import build_answer_extractability_issues

    # Todo bien: no genera ninguna issue.
    limpio = {
        "question_headings_count": 5, "question_headings_without_answer": 0,
        "answers_in_target_band": 3, "long_passages_over_250w": 0, "has_tldr_or_summary": False,
    }
    assert build_answer_extractability_issues("https://x.com/a", limpio) == []

    # Con huecos reales: sí genera, con el conteo exacto en el título.
    con_huecos = {
        "question_headings_count": 5, "question_headings_without_answer": 2,
        "answers_in_target_band": 1, "long_passages_over_250w": 1, "has_tldr_or_summary": False,
    }
    issues = build_answer_extractability_issues("https://x.com/b", con_huecos)
    assert len(issues) == 2
    assert all(i.category == "ai_extractability" for i in issues)
    assert "2 de 5" in issues[0].title


# ---------- Densidad de datos/hechos (§ mejoras 2026-08-02) ----------

def test_analyze_fact_density_detecta_numeros_datos_propios_y_claims_vagos():
    from backend.collectors.crawler import _analyze_fact_density

    html = """
    <html><body>
      <p>El cambio de pantalla cuesta $150.000 y toma aproximadamente 2 horas en total.</p>
      <p>Somos el mejor taller de reparación en toda la ciudad, sin ninguna duda posible.</p>
      <p>En nuestra encuesta encontramos que el 80% de los clientes vuelve a repararse con nosotros.</p>
      <a href="https://es.wikipedia.org/wiki/IPhone">Wikipedia</a>
      <a href="https://instagram.com/jc">Instagram</a>
    </body></html>
    """
    soup_result = _analyze_fact_density(_soup(html), "https://jcreparaciones.com/pagina")
    assert soup_result["vague_authority_claims_count"] == 1  # "el mejor... sin ninguna duda"
    assert soup_result["original_data_signal"] is True  # "en nuestra encuesta encontramos que"
    assert soup_result["authoritative_outbound_links"] == 1  # solo wikipedia, no instagram
    assert soup_result["numeric_density_per_100w"] > 0


def test_build_fact_density_issues_solo_reporta_claims_vagos():
    """§ Deliberadamente conservador: de las 5 métricas, solo
    vague_authority_claims_count genera una issue — las demás son datos, no
    defectos (ver docstring de build_fact_density_issues)."""
    from backend.analyzers.content import build_fact_density_issues

    sin_defectos = {
        "numeric_density_per_100w": 0.0, "passages_30w_plus_without_numbers": 5,
        "original_data_signal": False, "vague_authority_claims_count": 0, "authoritative_outbound_links": 0,
    }
    assert build_fact_density_issues("https://x.com/a", sin_defectos) == []

    con_claims_vagos = {**sin_defectos, "vague_authority_claims_count": 2}
    issues = build_fact_density_issues("https://x.com/b", con_claims_vagos)
    assert len(issues) == 1
    assert issues[0].category == "ai_extractability"
    assert "2 afirmación" in issues[0].title


# ---------- Frescura real: schema vs header HTTP (§ mejoras 2026-08-02) ----------

def test_extract_schema_dates_caso_real_jc():
    """Reproduce el caso real verificado en vivo contra
    jcreparaciones.com/reparar-vs-comprar-celular-2026: datePublished y
    dateModified reales en el JSON-LD, ambos 2026-06-02."""
    from backend.collectors.crawler import _extract_schema_dates

    html = """
    <html><body>
      <script type="application/ld+json">
      {"@type": "Article", "headline": "Reparar vs comprar",
       "datePublished": "2026-06-02", "dateModified": "2026-06-02"}
      </script>
    </body></html>
    """
    published, modified = _extract_schema_dates(_soup(html))
    assert published == "2026-06-02"
    assert modified == "2026-06-02"


def test_extract_schema_dates_sin_jsonld_devuelve_none():
    from backend.collectors.crawler import _extract_schema_dates

    published, modified = _extract_schema_dates(_soup("<html><body><p>sin schema</p></body></html>"))
    assert (published, modified) == (None, None)


def test_build_freshness_issues_detecta_mismatch_real():
    from backend.analyzers.content import build_freshness_issues

    # Schema dice "actualizado hoy", el servidor dice que no cambió en meses.
    issues = build_freshness_issues(
        "https://x.com/articulo",
        schema_date_modified="2026-08-20",
        http_last_modified="Mon, 02 Jun 2026 10:00:00 GMT",
    )
    assert len(issues) == 1
    assert issues[0].category == "freshness"
    assert "2026-08-20" in issues[0].title and "2026-06-02" in issues[0].title


def test_build_freshness_issues_no_reporta_si_coinciden():
    from backend.analyzers.content import build_freshness_issues

    issues = build_freshness_issues(
        "https://x.com/articulo",
        schema_date_modified="2026-06-02T10:00:00-05:00",
        http_last_modified="Tue, 02 Jun 2026 15:00:00 GMT",
    )
    assert issues == []


def test_build_freshness_issues_no_reporta_si_falta_cualquiera_de_los_dos():
    """Caso real de jc: el sitio (Next.js/Vercel) no manda header
    Last-Modified — sin ambas señales no hay nada que cruzar, no se opina."""
    from backend.analyzers.content import build_freshness_issues

    assert build_freshness_issues("https://x.com/a", "2026-06-02", None) == []
    assert build_freshness_issues("https://x.com/a", None, "Tue, 02 Jun 2026 15:00:00 GMT") == []
    assert build_freshness_issues("https://x.com/a", None, None) == []
