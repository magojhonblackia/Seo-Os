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
from backend.db.schema import ai_visibility_checks, backlinks, geo_grid_rankings, gsc_daily, gsc_queries, issues, keywords, pages, projects, scores, snapshots
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

    # § mejoras 2026-08-28: buscar "comparación" a secas ya no es único en la
    # página (la sección de renderizado JS también dice "Sin comparación
    # todavía") — se ancla a la sección de AI Visibility primero.
    section_idx = html.find("<h2>🧠 AI Visibility")
    assert section_idx != -1
    idx = html.find("comparación", section_idx)
    assert idx != -1, "debe mostrar la etiqueta 'comparación', no 'categoría'"
    snippet = html[idx:idx + 300]
    assert "no te menciona" not in snippet
    assert "no es señal" in snippet


@pytest.mark.asyncio
async def test_reporte_incluye_ai_visibility_comparativo():
    """§ mejoras 2026-08-26, pedido explícito del usuario: sobre las preguntas
    de categoría (ningún nombre en la pregunta), cuántas veces la IA te
    menciona a ti vs a cada competidor registrado."""
    project = _make_project("test-report-ai-visibility-comparativo")
    with get_connection() as conn:
        conn.execute(insert(ai_visibility_checks).values(
            project_id=project["id"], provider="deepseek", prompt_type="category",
            prompt="¿Cuál es el mejor taller de reparación de celulares en Cali?",
            response_text="Te recomiendo Capriservicios, buena reputación.",
            mentions_business=False, competitor_mentions=["capriservicios.com"], checked_at=now_iso(),
        ))
        conn.execute(insert(ai_visibility_checks).values(
            project_id=project["id"], provider="gemini", prompt_type="category",
            prompt="¿Cuál es el mejor taller de reparación de celulares en Cali?",
            response_text="Te recomiendo Capriservicios, buena reputación.",
            mentions_business=False, competitor_mentions=["capriservicios.com"], checked_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(project)

    section_idx = html.find("Comparativo (solo preguntas de categoría")
    assert section_idx != -1
    section = html[section_idx:section_idx + 600]
    assert "0 de 2" in section  # nunca nos mencionan en esta muestra
    assert "capriservicios.com" in section
    assert "🔴 más que tú" in section


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
async def test_reporte_keywords_muestra_hasta_40_no_solo_10():
    """Regresión real (2026-09-13): la sección solo mostraba el top 10 aunque
    GSC tuviera cientos de queries reales (caso real: jc con 149) — 139+
    keywords con posición/clics/impresiones reales nunca aparecían en el
    reporte, solo en la pestaña Rankings del dashboard."""
    project = _make_project("test-report-keywords-muchas")
    now = now_iso()
    with get_connection() as conn:
        for i in range(55):
            conn.execute(
                insert(gsc_queries).values(
                    project_id=project["id"], date="2026-07-10", query=f"keyword real numero {i}",
                    page="https://reporte-test.com/x", clicks=0, impressions=100 - i, ctr=0.0, position=20.0,
                    created_at=now,
                )
            )
    with _mock_provider():
        html = await generate_html_report(project)

    section_idx = html.find("<h2>🔑 Keywords Principales")
    assert section_idx != -1
    section = html[section_idx:section_idx + 6000]
    assert "keyword real numero 0" in section  # la de mayor impresiones, top 1
    assert "keyword real numero 39" in section  # la #40 por impresiones, todavía visible
    assert "keyword real numero 54" not in section  # más allá del top 40, no debe aparecer
    assert "Mostrando 40 de 55" in section


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


@pytest.mark.asyncio
async def test_reporte_incluye_ga4_contenido_preguntas_y_competidores():
    """Regresión real 2026-08-02: el mismo patrón de bug de arriba (datos
    reales en la DB que nunca llegaban al reporte) se repitió para 4
    secciones más — GA4, Contenido/E-E-A-T, Preguntas reales y Competidores —
    detectado porque el usuario preguntó "¿el reporte trae TODO lo que veo en
    el dashboard?" y la respuesta honesta era que no."""
    project = _make_project("test-report-ga4-contenido-competidores")
    pid = project["id"]

    with get_connection() as conn:
        # Contenido / E-E-A-T
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://reporte-test.com/legible-baja",
                first_seen=now_iso(), word_count=500, readability_score=42,
                eeat_score=60, has_author=False, has_date=False,
            )
        )
        # Preguntas reales (source='question_ideas', mismo formato que
        # persist_question_ideas() en backend/collectors/question_ideas.py)
        conn.execute(
            insert(keywords).values(
                project_id=pid, keyword="por que mi celular no prende",
                source="question_ideas", volume=None,
                trend_data={"seed_keyword": "celular no prende", "already_has_real_data": True},
                intent=None, last_updated=now_iso(),
            )
        )
        # GA4
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="ga4", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "days": 30, "conversion_metric": "keyEvents",
                    "rows": [{"landing_page": "/", "sessions": 120, "conversions": 4.0}],
                },
                created_at=now_iso(),
            )
        )
        # Competidor registrado + escaneado
        conn.execute(
            projects.update().where(projects.c.id == pid).values(competitors=["competidor-real.com"])
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:competidor-real.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages_crawled": 12, "technical_score": 70, "geo_score": 50, "sample_keywords": []},
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-ga4-contenido-competidores"))

    for titulo in ["GA4 — conversiones", "Contenido y E-E-A-T", "Preguntas reales de Google", "Competidores</h2>"]:
        assert titulo in html, f"falta la sección: {titulo}"

    assert "120" in html and "keyEvents" in html            # GA4 real
    assert "reporte-test.com/legible-baja" in html            # página real
    assert "por que mi celular no prende" in html             # pregunta real
    assert "competidor-real.com" in html and "12" in html     # competidor real


@pytest.mark.asyncio
async def test_reporte_incluye_extractabilidad_y_densidad_de_datos():
    """Regresión real 2026-08-02: el usuario preguntó si el reporte ya
    registraba lo nuevo (M11/M12/M13, ver README) — las ISSUES de esos
    módulos ya salían en el Action Plan, pero el dato crudo por página
    (cuántas preguntas, densidad numérica, frescura) no tenía sección propia,
    a diferencia de Contenido/E-E-A-T que sí la tiene."""
    project = _make_project("test-report-extractabilidad")
    with get_connection() as conn:
        conn.execute(
            insert(pages).values(
                project_id=project["id"], url="https://reporte-test.com/preguntas",
                first_seen=now_iso(), word_count=800,
                question_headings_count=5, question_headings_without_answer=2,
                question_headings_answered_with_cta_only=1,
                answers_in_target_band=1, long_passages_over_250w=1, has_tldr_or_summary=False,
                numeric_density_per_100w=4.5, passages_30w_plus_without_numbers=2,
                original_data_signal=True, vague_authority_claims_count=1,
                authoritative_outbound_links=3,
                schema_date_modified="2026-08-01", http_last_modified="Mon, 01 Aug 2026 10:00:00 GMT",
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-extractabilidad"))

    section_idx = html.find("<h2>🧠 Extraíbilidad por IA y densidad de datos")
    assert section_idx != -1
    idx = html.find("reporte-test.com/preguntas", section_idx)
    assert idx != -1
    row = html[idx:idx + 400]
    assert "5 (2 / 1)" in row   # 5 preguntas, 2 sin respuesta, 1 solo CTA
    assert "4.5" in row         # densidad numérica
    assert "✅ coincide" in row  # schema y header coinciden (mismo 2026-08-01)


@pytest.mark.asyncio
async def test_reporte_incluye_share_of_voice():
    """§ mejoras 2026-08-25 (comparación con Semrush): Share of Voice se
    calcula en run_opportunities_analysis y se guarda en el mismo snapshot que
    el CTR — el reporte debe leerlo de ahí, con la misma honestidad de
    'sin muestra suficiente' cuando corresponda."""
    project = _make_project("test-report-share-of-voice")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="opportunities", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "issues_found": 0,
                    "share_of_voice": {
                        "available": True,
                        "share_of_voice_pct": 42.5,
                        "keywords_included": 7,
                        "best_bucket_ctr": 0.10,
                        "ctr_by_bucket": {"1-3": 0.10, "4-10": 0.02},
                        "note": "Basado en el CTR real de este sitio por tramo de posición, no en una curva de industria",
                    },
                },
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-share-of-voice"))

    assert "Share of Voice" in html
    assert "42.5%" in html
    assert "7 keyword" in html
    assert "curva de industria" in html


@pytest.mark.asyncio
async def test_reporte_share_of_voice_declara_muestra_insuficiente():
    project = _make_project("test-report-sov-insuficiente")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="opportunities", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "issues_found": 0,
                    "share_of_voice": {"available": False, "reason": "Solo 2 clic(s): no alcanza para comparar."},
                },
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-sov-insuficiente"))

    section_idx = html.find("<h2>📢 Share of Voice")
    assert section_idx != -1
    assert "no alcanza" in html[section_idx:section_idx + 500]


@pytest.mark.asyncio
async def test_reporte_incluye_headers_de_seguridad():
    """§ mejoras 2026-08-25: security_headers.py se ejecutaba en cada auditoría
    pero solo generaba una issue cuando algo faltaba — con todo bien
    configurado (caso real de jc: HSTS+CSP+etc. presentes) el reporte no decía
    nada al respecto. El dato crudo necesita sección propia, igual que CTR."""
    project = _make_project("test-report-security-headers")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="security_headers", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "url": "https://reporte-test.com",
                    "checked_status": 200,
                    "analysis": {
                        "hsts": {"present": True, "value": "max-age=31536000", "max_age": 31536000, "weak": False},
                        "csp": {"present": True, "value": "default-src 'self'", "unsafe_tokens": [], "has_frame_ancestors": False},
                        "x_frame_options": {"present": False, "value": None, "covered_by_csp": False},
                        "x_content_type_options": {"present": True, "value": "nosniff"},
                        "referrer_policy": {"present": True, "value": "strict-origin-when-cross-origin"},
                        "permissions_policy": {"present": False, "value": None},
                    },
                },
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-security-headers"))

    section_idx = html.find("<h2>🔒 Headers de seguridad HTTP")
    assert section_idx != -1
    section = html[section_idx:section_idx + 1200]
    assert "max-age=31536000" in section
    assert "X-Frame-Options</td><td>❌" in section
    assert "Permissions-Policy</td><td>❌" in section


@pytest.mark.asyncio
async def test_reporte_incluye_indexnow_no_configurado():
    """§ mejoras 2026-08-25: el check de IndexNow corría en cada auditoría
    (status='skipped' cuando no hay INDEXNOW_KEY) pero no tenía NINGUNA
    exposición — ni endpoint ni sección de reporte."""
    project = _make_project("test-report-indexnow")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="indexnow", status="skipped",
                started_at=now_iso(), finished_at=now_iso(),
                error_message="IndexNow sin configurar. Pasos: 1) agrega INDEXNOW_KEY=...",
                raw_data={"configured": False, "suggested_key": "abc123"},
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-indexnow"))

    section_idx = html.find("<h2>📡 IndexNow (Bing")
    assert section_idx != -1
    assert "No configurado todavía" in html[section_idx:section_idx + 500]


@pytest.mark.asyncio
async def test_reporte_incluye_comparacion_detallada_de_competidor():
    """§ mejoras 2026-08-25: build_competitor_comparison() (schema_gap +
    comparación de E-E-A-T/contenido) ya existía y tenía datos reales
    (verificado en vivo contra jc: ifixit.com con schema_gap=['NewsArticle']),
    pero solo se exponía bajo demanda vía /competitors/{domain}/detail — la
    matriz del reporte nunca mostraba el detalle, se quedaba afuera."""
    project = _make_project("test-report-competitor-detail")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://reporte-test.com/pagina",
                first_seen=now_iso(), word_count=1000, schema_types=["LocalBusiness"],
                title="Título", meta_description="Meta", has_author=True, has_date=True, has_contact=True,
            )
        )
        conn.execute(
            projects.update().where(projects.c.id == pid).values(competitors=["competidor-detalle.com"])
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:competidor-detalle.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "pages_crawled": 5, "technical_score": 80, "geo_score": 60,
                    "schema_coverage": {"LocalBusiness": 5, "NewsArticle": 3},
                    "avg_word_count": 500, "avg_title_length": 40, "avg_meta_length": 120,
                    "eeat_signals": {"has_author_pct": 0, "has_date_pct": 20, "has_contact_pct": 0},
                    "sample_keywords": [],
                },
                created_at=now_iso(),
            )
        )

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-competitor-detail"))

    idx = html.find("competidor-detalle.com")
    assert idx != -1
    section = html[idx:idx + 800]
    assert "NewsArticle" in section  # schema_gap real: ellos lo usan, nosotros no
    assert "1000" in section or "1000.0" in section  # nuestro avg_word_count real


@pytest.mark.asyncio
async def test_reporte_tecnico_desglosa_por_tipo_de_check():
    """§ mejoras 2026-08-25: el resumen 'Técnico' sumaba las 8 columnas del
    semáforo (title, meta, h1, schema, og, canonical, indexable, x_robots) en
    un solo conteo aplanado — no dejaba ver SI el problema estaba concentrado
    en un check o disperso entre varios. Regresión: una página no-indexable
    debe aparecer como rojo en 'Indexabilidad' pero NO afectar el conteo de
    'Schema' (checks independientes, no un solo número global)."""
    project = _make_project("test-report-tecnico-por-check")
    pid = project["id"]
    common = dict(
        project_id=pid, first_seen=now_iso(),
        title="Reparación de pantallas de iPhone en Cali",
        meta_description="Reparamos pantallas de iPhone y Samsung en Cali con garantía de 6 meses. Agenda tu cita hoy mismo, precios claros.",
        h1="Reparación de iPhone en Cali", schema_types=["LocalBusiness"],
        og={"title": "x", "description": "y", "image": "z"},
    )
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            **common, url="https://reporte-test.com/indexable",
            canonical="https://reporte-test.com/indexable", is_indexable=True,
        ))
        conn.execute(insert(pages).values(
            **common, url="https://reporte-test.com/no-indexable",
            canonical="https://reporte-test.com/no-indexable", is_indexable=False,
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-tecnico-por-check"))

    idx = html.find("<h2>🔧 Técnico</h2>")
    assert idx != -1
    section = html[idx:idx + 1500]
    assert "<td>Indexabilidad</td><td>🟢 1</td><td>🟡 0</td><td>🔴 1</td>" in section
    assert "<td>Schema</td><td>🟢 2</td><td>🟡 0</td><td>🔴 0</td>" in section


@pytest.mark.asyncio
async def test_reporte_incluye_backlinks_toxicos():
    """§ mejoras 2026-08-26: toxic_backlinks_report() ya existía (spec del
    usuario) y tenía endpoint propio, pero no llegaba al reporte estático —
    mismo patrón de bug que Share of Voice al principio de la sesión."""
    project = _make_project("test-report-backlinks-toxicos")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(backlinks).values(
            project_id=pid, source_url="https://spam-network.xyz/pagina",
            source_domain="spam-network.xyz", target_url="https://reporte-test.com/",
            anchor_text="buy backlinks cheap seo", source="bing",
            domain_authority=None, spam_score=None, is_toxic=False,
            first_seen=now_iso(), last_seen=now_iso(), status="active",
        ))
        for i in range(10):
            conn.execute(insert(backlinks).values(
                project_id=pid, source_url=f"https://real{i}.com/pagina",
                source_domain=f"real{i}.com", target_url="https://reporte-test.com/",
                anchor_text="Reporte Test SAS", source="bing",
                domain_authority=None, spam_score=None, is_toxic=False,
                first_seen=now_iso(), last_seen=now_iso(), status="active",
            ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-backlinks-toxicos"))

    section_idx = html.find("<h2>☠️ Backlinks tóxicos por patrón")
    assert section_idx != -1
    section = html[section_idx:section_idx + 1000]
    assert "spam-network.xyz" in section
    assert "toxic_anchor" in section
    assert "Agregar a disavow" in section
    assert "11 backlinks" in section  # 1 tóxico + 10 limpios


@pytest.mark.asyncio
async def test_reporte_backlinks_toxicos_declara_muestra_insuficiente():
    project = _make_project("test-report-backlinks-toxicos-insuficiente")
    with get_connection() as conn:
        conn.execute(insert(backlinks).values(
            project_id=project["id"], source_url="https://x.com/p", source_domain="x.com",
            target_url="https://reporte-test.com/", anchor_text="ok", source="bing",
            domain_authority=None, spam_score=None, is_toxic=False,
            first_seen=now_iso(), last_seen=now_iso(), status="active",
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-backlinks-toxicos-insuficiente"))

    section_idx = html.find("<h2>☠️ Backlinks tóxicos por patrón")
    assert section_idx != -1
    assert "no alcanza" in html[section_idx:section_idx + 500]


@pytest.mark.asyncio
async def test_reporte_incluye_gap_real_de_keywords_vs_competidor():
    """§ mejoras 2026-08-28: get_competitor_keyword_gaps() (DataForSEO Labs)
    tenía endpoint propio pero no llegaba al reporte estático — mismo patrón
    de bug que Share of Voice y Backlinks tóxicos antes."""
    project = _make_project("test-report-competitor-keyword-gaps")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(projects.update().where(projects.c.id == pid).values(competitors=["competidor-real.com"]))
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="competitor_keywords:competidor-real.com", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={
                "domain": "competidor-real.com",
                "keywords": [{"keyword": "a", "position": 1}, {"keyword": "b", "position": 2}],
                "gaps": [{"keyword": "cambio pantalla samsung cali", "position": 7, "search_volume": 90}],
            },
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-competitor-keyword-gaps"))

    section_idx = html.find("<h2>🔑 Gap real de keywords vs competidor")
    assert section_idx != -1
    section = html[section_idx:section_idx + 800]
    assert "competidor-real.com" in section
    assert "cambio pantalla samsung cali" in section
    assert "#7" in section
    assert "90" in section


@pytest.mark.asyncio
async def test_reporte_gap_de_keywords_declara_no_consultado_sin_snapshot():
    project = _make_project("test-report-competitor-keyword-gaps-vacio")
    with get_connection() as conn:
        conn.execute(projects.update().where(projects.c.id == project["id"]).values(competitors=["nunca-consultado.com"]))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-competitor-keyword-gaps-vacio"))

    section_idx = html.find("<h2>🔑 Gap real de keywords vs competidor")
    assert section_idx != -1
    section = html[section_idx:section_idx + 500]
    assert "nunca-consultado.com" in section
    assert "Nunca consultado" in section


@pytest.mark.asyncio
async def test_reporte_incluye_competidores_descubiertos():
    """§ mejoras 2026-09-13: descubrir competidores reales por overlap de
    keywords (DataForSEO Labs competitors_domain) — usa el caso real
    verificado en vivo contra jc (celudigital.com.co, posición promedio 3.95,
    instagram.com/facebook.com excluidos como plataformas)."""
    project = _make_project("test-report-competitor-discovery")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="competitor_discovery", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={
                "discovered": [{"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42}],
                "platforms_excluded": ["instagram.com", "facebook.com"],
                "analyzed": [{
                    "domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42,
                    "keywords_found": 1,
                    "top_keywords": [{"keyword": "reparar celular cali", "position": 3, "search_volume": 200}],
                    "gaps": [{"keyword": "gap real de celudigital", "position": 5, "search_volume": 90}],
                    "error": None,
                }],
                "already_tracked": [],
            },
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-competitor-discovery"))

    section_idx = html.find("<h2>🔍 Descubrir competidores reales")
    assert section_idx != -1
    section = html[section_idx:section_idx + 1500]
    assert "celudigital.com.co" in section
    assert "4.0" in section  # avg_position 3.95 redondeado a 1 decimal (mismo criterio que el resto del reporte)
    assert "reparar celular cali" in section
    assert "gap real de celudigital" in section
    assert "instagram.com" in section  # aparece en la lista de plataformas excluidas
    assert "NO registrado" in section  # no está en already_tracked


@pytest.mark.asyncio
async def test_reporte_competidores_descubiertos_declara_sin_datos():
    project = _make_project("test-report-competitor-discovery-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-competitor-discovery-vacio"))

    section_idx = html.find("<h2>🔍 Descubrir competidores reales")
    assert section_idx != -1
    assert "Sin búsqueda todavía" in html[section_idx:section_idx + 300]


@pytest.mark.asyncio
async def test_reporte_incluye_geo_grid():
    """§ mejoras 2026-08-28: geo-grid rank tracking (DataForSEO Maps SERP,
    pedido explícito del usuario) — visibilidad por punto geográfico, no un
    solo punto como Local Pack."""
    project = _make_project("test-report-geo-grid")
    pid = project["id"]
    with get_connection() as conn:
        # 3 de 4 puntos con nosotros visibles -> 75% de visibilidad
        for lat, lng, pos in [(3.44, -76.54, 2), (3.44, -76.52, 3), (3.46, -76.54, 1), (3.46, -76.52, None)]:
            conn.execute(insert(geo_grid_rankings).values(
                project_id=pid, keyword="reparacion celulares cali", date="2026-08-28",
                grid_lat=lat, grid_lng=lng, our_position=pos, our_listing_title="JC Reparaciones" if pos else None,
                checked_at=now_iso(),
            ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-geo-grid"))

    section_idx = html.find("<h2>📍 Geo-grid rank tracking")
    assert section_idx != -1
    section = html[section_idx:section_idx + 500]
    assert "reparacion celulares cali" in section
    assert "75.0%" in section
    assert "3/4 puntos" in section


@pytest.mark.asyncio
async def test_reporte_geo_grid_declara_sin_datos():
    project = _make_project("test-report-geo-grid-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-geo-grid-vacio"))

    section_idx = html.find("<h2>📍 Geo-grid rank tracking")
    assert section_idx != -1
    assert "Sin datos" in html[section_idx:section_idx + 300]


@pytest.mark.asyncio
async def test_reporte_incluye_js_rendering_check():
    """§ mejoras 2026-08-28: comparación crawler propio vs renderizado JS
    (DataForSEO On-Page), pedido explícito del usuario. Usa la forma real de
    datos verificada en vivo contra jc (crudo > renderizado, sin gap)."""
    project = _make_project("test-report-js-rendering")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="js_rendering_check", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={"results": [
                {"page_id": 1, "url": "https://reporte-test.com/", "raw_word_count": 3301,
                 "rendered_word_count": 2187, "gap_pct": -33.7, "significant_gap": False,
                 "raw_title": "T", "rendered_title": "T", "title_matches": True},
            ]},
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-js-rendering"))

    section_idx = html.find("<h2>🖥️ Crawler propio vs renderizado con JS")
    assert section_idx != -1
    section = html[section_idx:section_idx + 900]
    assert "3301" in section
    assert "2187" in section
    assert "Ningún gap significativo" in section


@pytest.mark.asyncio
async def test_reporte_incluye_volumen_de_busqueda_real():
    """§ mejoras 2026-08-29: volumen de búsqueda real de Google Ads (DataForSEO
    Keywords Data API). Usa la forma real verificada en vivo contra jc."""
    project = _make_project("test-report-search-volume")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="search_volume", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={"results": [
                {"keyword": "reparacion de celulares cali", "search_volume": 170,
                 "competition": "LOW", "competition_index": 2, "cpc": None, "monthly_searches": []},
            ]},
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-search-volume"))

    section_idx = html.find("<h2>📊 Volumen de búsqueda real")
    assert section_idx != -1
    section = html[section_idx:section_idx + 700]
    assert "reparacion de celulares cali" in section
    assert "170" in section
    assert "LOW" in section


@pytest.mark.asyncio
async def test_reporte_volumen_de_busqueda_declara_sin_datos():
    project = _make_project("test-report-search-volume-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-search-volume-vacio"))

    section_idx = html.find("<h2>📊 Volumen de búsqueda real")
    assert section_idx != -1
    assert "Sin consulta todavía" in html[section_idx:section_idx + 300]


@pytest.mark.asyncio
async def test_reporte_incluye_business_listings():
    """§ mejoras 2026-08-29: citas locales / listados de negocio (DataForSEO
    Business Listings API). Usa el caso real verificado en vivo contra jc:
    listado propio + un posible duplicado en la misma ciudad."""
    project = _make_project("test-report-business-listings")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="business_listings", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={
                "business_title": "JC Reparaciones",
                "total_items_found": 2,
                "classification": {
                    "own_listing": {"title": "JC REPARACIONES", "address": "Av. 5a Nte. #20-13, Cali", "phone": "+573163333400", "domain": "jcreparaciones.com"},
                    "citations": [],
                    "possible_duplicates": [
                        {"title": "Jc Reparaciones", "address": "Cra. 103 #13 L, Ciudad Jardín, Cali", "is_claimed": False},
                    ],
                },
            },
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-business-listings"))

    section_idx = html.find("<h2>📍 Citas locales")
    assert section_idx != -1
    section = html[section_idx:section_idx + 1200]
    assert "JC REPARACIONES" in section
    assert "Ciudad Jardín" in section
    assert "posible" in section.lower()


@pytest.mark.asyncio
async def test_reporte_business_listings_declara_sin_datos():
    project = _make_project("test-report-business-listings-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-business-listings-vacio"))

    section_idx = html.find("<h2>📍 Citas locales")
    assert section_idx != -1
    assert "Sin consulta todavía" in html[section_idx:section_idx + 300]


@pytest.mark.asyncio
async def test_reporte_js_rendering_declara_sin_datos():
    project = _make_project("test-report-js-rendering-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-js-rendering-vacio"))

    section_idx = html.find("<h2>🖥️ Crawler propio vs renderizado con JS")
    assert section_idx != -1
    assert "Sin comparación todavía" in html[section_idx:section_idx + 300]


@pytest.mark.asyncio
async def test_reporte_incluye_ai_overview_real():
    """§ mejoras 2026-08-28: AI Overview real de Google (SERP en vivo), no
    una llamada directa a un LLM. Usa la forma de datos real verificada en
    vivo contra jc (ver docstring de ai_overview_check.py)."""
    project = _make_project("test-report-ai-overview")
    pid = project["id"]
    with get_connection() as conn:
        conn.execute(insert(snapshots).values(
            project_id=pid, collector="ai_overview_check", status="ok",
            started_at=now_iso(), finished_at=now_iso(),
            raw_data={"results": [
                {"keyword": "reparacion iphone cali", "has_ai_overview": False, "brand_cited": False, "references": []},
                {"keyword": "por que mi iphone se calienta", "has_ai_overview": True, "brand_cited": False,
                 "references": [{"source": "Apple Support", "title": "x"}, {"source": "MacServiceBcn", "title": "y"}]},
            ]},
            created_at=now_iso(),
        ))

    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-ai-overview"))

    section_idx = html.find("<h2>🌐 AI Overview real de Google")
    assert section_idx != -1
    section = html[section_idx:section_idx + 1000]
    assert "1 de 2" in section
    assert "por que mi iphone se calienta" in section
    assert "🔴 no" in section
    assert "Apple Support" in section and "MacServiceBcn" in section


@pytest.mark.asyncio
async def test_reporte_ai_overview_declara_sin_datos():
    project = _make_project("test-report-ai-overview-vacio")
    with _mock_provider():
        html = await generate_html_report(get_project_or_404("test-report-ai-overview-vacio"))

    section_idx = html.find("<h2>🌐 AI Overview real de Google")
    assert section_idx != -1
    assert "Sin consulta todavía" in html[section_idx:section_idx + 300]
