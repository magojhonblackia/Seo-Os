"""Tests del reporte HTML/PDF (Fase 4): debe generar HTML válido tanto para
un proyecto sin ningún dato (todo en 'empty_reason') como para uno con datos
reales, sin lanzar excepciones en ningún caso. Las llamadas a DeepSeek se
mockean SIEMPRE (regla QA: nunca red real en la suite, aunque DEEPSEEK_API_KEY
esté configurada de verdad en este entorno)."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.api.deps import get_project_or_404
from backend.db.database import get_connection, now_iso
from backend.db.schema import ai_visibility_checks, gsc_daily, gsc_queries, issues, pages, projects, scores, snapshots
from backend.main import app
from backend.reports import generate_html_report

client = TestClient(app)


def _make_project(slug: str) -> dict:
    with get_connection() as conn:
        conn.execute(
            insert(projects).values(
                slug=slug, name="Reporte Test SAS", url="https://reporte-test.com",
                gsc_property="sc-domain:reporte-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        )
    return get_project_or_404(slug)


def _fake_ai_response(content: str):
    response = AsyncMock()
    response.content = content
    return response


def _mock_provider(content: str = "- Punto uno real\n- Punto dos real"):
    """AI mockeada: nunca llama a DeepSeek de verdad en la suite."""
    provider = AsyncMock()
    provider.chat.return_value = _fake_ai_response(content)
    return patch("backend.ai.engine.get_provider", return_value=provider)


# ---------- Sin IA (DeepSeek no disponible) ----------

@pytest.mark.asyncio
async def test_reporte_html_sin_ningun_dato_no_lanza():
    project = _make_project("test-report-vacio")
    with patch("backend.ai.engine.get_provider", side_effect=Exception("DeepSeek no configurado")):
        html = await generate_html_report(project)
    assert "<html" in html
    assert "Reporte Test SAS" in html


@pytest.mark.asyncio
async def test_reporte_degrada_sin_ia_muestra_hechos_crudos():
    """Regla S3: si DeepSeek falla o no está configurado, el reporte se genera
    igual con los hechos crudos, nunca se rompe."""
    project = _make_project("test-report-sin-ia")
    with patch("backend.ai.engine.get_provider", side_effect=Exception("sin key")):
        html = await generate_html_report(project)
    assert "DeepSeek no está configurado" in html or "sin narrar" in html


@pytest.mark.asyncio
async def test_reporte_html_incluye_nombre_y_url():
    project = _make_project("test-report-nombre")
    with _mock_provider():
        html = await generate_html_report(project)
    assert "Reporte Test SAS" in html
    assert "reporte-test.com" in html


@pytest.mark.asyncio
async def test_reporte_sin_crawl_avisa_que_falta_crawlear():
    """Si el proyecto no tiene páginas crawleadas, el reporte lo dice explícito
    en vez de mostrar datos vacíos como si fueran frescos (§ frescura 2026-07-23)."""
    project = _make_project("test-report-sin-crawl")
    with _mock_provider():
        html = await generate_html_report(project)
    assert "aún no ha sido crawleado" in html


@pytest.mark.asyncio
async def test_reporte_muestra_fecha_del_ultimo_crawl_distinta_de_generado():
    """El reporte es una VISTA del último crawl, no crawlea al generarse — debe
    mostrar 'Último crawl del sitio: <fecha>' para no confundirlo con la hora de
    generación (bug reportado: 3 reportes distintos, mismo dato viejo)."""
    project = _make_project("test-report-crawl-date")
    with get_connection() as conn:
        conn.execute(
            insert(pages).values(
                project_id=project["id"], url="https://reporte-test.com/x",
                first_seen=now_iso(), last_crawled="2026-07-20T10:30:00+00:00",
                title="Página de prueba", is_indexable=True,
            )
        )
    with _mock_provider():
        html = await generate_html_report(project)
    assert "Último crawl del sitio: 2026-07-20 10:30" in html
    assert "generar el reporte" in html  # el aviso de re-crawlear para datos frescos


@pytest.mark.asyncio
async def test_reporte_html_incluye_boton_imprimir():
    project = _make_project("test-report-print")
    with _mock_provider():
        html = await generate_html_report(project)
    assert "window.print()" in html


@pytest.mark.asyncio
async def test_reporte_html_muestra_issue_critica_real():
    project = _make_project("test-report-issue-critica")
    now = now_iso()
    with get_connection() as conn:
        snap_id = conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="crawler", status="ok",
                started_at=now, finished_at=now, raw_data={}, created_at=now,
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(issues).values(
                project_id=project["id"], page_id=None, snapshot_id=snap_id,
                severity="critical", category="technical", title="Hallazgo crítico de prueba",
                status="open", detected_at=now,
            )
        )

    with _mock_provider():
        html = await generate_html_report(project)
    assert "Hallazgo crítico de prueba" in html


@pytest.mark.asyncio
async def test_reporte_escapa_html_hostil_en_titulo_de_issue():
    """El título de una issue puede venir de contenido crawleado de terceros
    (regla §4.3: contenido hostil) — nunca debe inyectarse sin escapar."""
    project = _make_project("test-report-xss")
    now = now_iso()
    with get_connection() as conn:
        snap_id = conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="crawler", status="ok",
                started_at=now, finished_at=now, raw_data={}, created_at=now,
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(issues).values(
                project_id=project["id"], page_id=None, snapshot_id=snap_id,
                severity="high", category="technical", title="<script>alert(1)</script>",
                status="open", detected_at=now,
            )
        )

    with _mock_provider():
        html = await generate_html_report(project)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.asyncio
async def test_reporte_muestra_seccion_lo_que_no_medimos():
    """La honestidad explícita debe estar siempre presente, no solo cuando
    falta algo puntual."""
    project = _make_project("test-report-honestidad")
    with _mock_provider():
        html = await generate_html_report(project)
    assert "Lo que NO medimos todavía" in html
    assert "Volumen de búsqueda absoluto" in html
    assert "ChatGPT (OpenAI)" in html


@pytest.mark.asyncio
async def test_reporte_ai_visibility_no_trata_comparison_como_categoria():
    """Bug real 2026-07-27: los prompts de tipo 'comparison' (§ lista curada
    del usuario, "¿JC Reparaciones o Capri Servicios, cuál es mejor?") caían
    en la rama de 'categoría' del render y mostraban 'no te menciona' como si
    fuera una señal real — pero el nombre ya está en la pregunta de
    comparación (igual que en marca), así que no lo es."""
    project = _make_project("test-report-ai-visibility-comparison")
    with get_connection() as conn:
        conn.execute(
            insert(ai_visibility_checks).values(
                project_id=project["id"], provider="deepseek", prompt_type="comparison",
                prompt="¿Reporte Test SAS o Competidor X, cuál es mejor?",
                response_text="Reporte Test SAS tiene buena reputación.",
                mentions_business=None, checked_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(project)

    idx = html.find("comparación")
    assert idx != -1, "debe mostrar la etiqueta 'comparación', no 'categoría'"
    snippet = html[idx:idx + 300]
    assert "no te menciona" not in snippet
    assert "no es señal" in snippet


@pytest.mark.asyncio
async def test_seccion_de_honestidad_no_declara_no_medible_lo_que_ya_medimos():
    """Regresión real 2026-07-25: la sección seguía diciendo que las reseñas de
    Google y la posición de competidores eran imposibles de medir, cuando ya se
    miden vía Serper (/places y el top-10). Un reporte que SUBESTIMA sus
    capacidades desinforma igual que uno que las exagera — y este texto se pega
    en una IA, que lo tomará como verdad."""
    project = _make_project("test-report-honestidad-vigente")
    with _mock_provider():
        html = await generate_html_report(project)

    stale_claims = [
        "Reseñas de Google (conteo/rating):</strong> requiere",
        "Custom Search JSON API",
        "no hay fuente gratuita para esto",
    ]
    for claim in stale_claims:
        assert claim not in html, f"El reporte declara como no-medible algo que ya medimos: {claim!r}"


@pytest.mark.asyncio
async def test_reporte_incluye_local_pack_y_serp_real_con_datos():
    """Lo que se mide tiene que LLEGAR al reporte: es el texto que se pega en
    una IA, y un hallazgo que no aparece ahí es como si no existiera."""
    from backend.db.schema import local_pack_rankings, serp_results

    project = _make_project("test-report-serp-localpack")
    today = now_iso()[:10]
    with get_connection() as conn:
        conn.execute(
            insert(local_pack_rankings).values(
                project_id=project["id"], keyword="reparacion test", date=today,
                our_position=3, our_listing_title="Reporte Test SAS",
                our_rating=4.6, our_reviews_count=88, checked_at=now_iso(),
            )
        )
        conn.execute(
            insert(serp_results),
            [
                {
                    "project_id": project["id"], "keyword": "reparacion test", "date": today,
                    "position": 1, "url": "https://rival-real.com/x", "domain": "rival-real.com",
                    "title": "Rival", "snippet": "s", "is_ours": False, "created_at": now_iso(),
                },
                {
                    "project_id": project["id"], "keyword": "reparacion test", "date": today,
                    "position": 2, "url": "https://reporte-test.com/y", "domain": "reporte-test.com",
                    "title": "Nosotros", "snippet": "s", "is_ours": True, "created_at": now_iso(),
                },
            ],
        )

    with _mock_provider():
        html = await generate_html_report(project)

    assert "Local Pack de Google Maps" in html
    assert "4.6" in html and "88 reseñas" in html
    assert "SERP real" in html
    assert "rival-real.com" in html


@pytest.mark.asyncio
async def test_reporte_keywords_reales_de_gsc():
    project = _make_project("test-report-keywords")
    now = now_iso()
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=project["id"], date="2026-07-10", query="reparacion iphone cali test",
                page="https://reporte-test.com/iphone", clicks=5, impressions=80, ctr=0.0625, position=14.3,
                created_at=now,
            )
        )
    with _mock_provider():
        html = await generate_html_report(project)
    assert "reparacion iphone cali test" in html


@pytest.mark.asyncio
async def test_reporte_oportunidad_cerca_del_top10_real():
    """Keyword en posición 11-20 con impresiones reales debe aparecer en la
    sección de oportunidades — a diferencia de 'posición estimada' inventada."""
    project = _make_project("test-report-oportunidad")
    now = now_iso()
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=project["id"], date="2026-07-10", query="keyword cerca del top10",
                page="https://reporte-test.com/x", clicks=0, impressions=200, ctr=0.0, position=13.5,
                created_at=now,
            )
        )
    with _mock_provider():
        html = await generate_html_report(project)
    assert "keyword cerca del top10" in html


@pytest.mark.asyncio
async def test_reporte_muestra_delta_de_score_real():
    """Dos mediciones reales de score deben mostrar un delta 'vs anterior' —
    nunca 'vs promedio del sector' (ese dato no existe públicamente)."""
    project = _make_project("test-report-delta")
    with get_connection() as conn:
        conn.execute(insert(scores).values(project_id=project["id"], date="2026-07-01", kind="geo", value=60, breakdown={}))
        conn.execute(insert(scores).values(project_id=project["id"], date="2026-07-08", kind="geo", value=90, breakdown={}))
    with _mock_provider():
        html = await generate_html_report(project)
    assert "vs promedio del sector" not in html
    assert "vs anterior" in html


# ---------- API ----------

def test_api_report_devuelve_html():
    _make_project("test-report-api")
    with _mock_provider():
        resp = client.get("/api/dashboard/test-report-api/report")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Reporte Test SAS" in resp.text


def test_api_report_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/report")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reporte_incluye_las_secciones_de_datos_medidos():
    """Regresión: el reporte tenía CWV, indexación, cobertura y ranking real
    guardados en la DB pero solo aparecían como fragmentos sueltos dentro del
    Action Plan. Un dato que se mide y no llega al reporte es como si no se
    midiera — y este texto es el que se pega en una IA."""
    from backend.db.schema import indexation_status, pagespeed, serp_rankings, snapshots

    project = _make_project("test-report-secciones-datos")
    today = now_iso()[:10]
    with get_connection() as conn:
        conn.execute(
            insert(pagespeed).values(
                project_id=project["id"], date=today, strategy="mobile",
                url="https://reporte-test.com/", performance_score=71,
                lcp_ms=3100, cls=0.04, tbt_ms=120, field_data_available=False,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(indexation_status).values(
                project_id=project["id"], url="https://reporte-test.com/sin-indexar",
                verdict="NEUTRAL", coverage_state="URL is unknown to Google",
                checked_at=now_iso(),
            )
        )
        conn.execute(
            insert(serp_rankings).values(
                project_id=project["id"], keyword="kw de prueba", date=today,
                our_position=7, our_url="https://reporte-test.com/",
                competitor_positions={}, serp_features={}, checked_at=now_iso(),
            )
        )
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="site_health", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "coverage": {
                        "counts": {"sitemap": 300, "crawled": 90, "indexed": 40, "inspected": 60},
                        "orphans": [], "broken": [], "redirects": [],
                        "robots_sitemap_conflicts": [], "sitemap_not_inspected": ["x"],
                    },
                    "internal_links": {"weak": [], "deep": [], "per_page": []},
                    "duplicates": {"titles": [], "metas": [], "h1s": [], "thin": []},
                },
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(project)

    # Las 4 secciones existen…
    for titulo in ["Core Web Vitals por página", "Cobertura: sitemap",
                   "Indexación real en Google", "Ranking real en Google"]:
        assert titulo in html, f"falta la sección: {titulo}"

    # …y traen el dato real, no un cascarón vacío
    assert "3100" in html                      # LCP medido
    assert "URL is unknown to Google" in html  # veredicto textual de Google
    assert "300" in html and "40" in html      # triángulo de cobertura
    assert "#7" in html                        # posición real en el SERP
