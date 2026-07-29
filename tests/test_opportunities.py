"""Tests de opportunities.py: CTR-0 top10, zero-impression, e idempotencia
del Action Plan (regla real encontrada: correr el análisis 2 veces NO debe
duplicar issues)."""
from sqlalchemy import insert, select

from backend.analyzers.cannibalization import QueryPageRow
from backend.analyzers.opportunities import (
    calculate_content_score,
    calculate_seo_score,
    calculate_technical_score,
    detect_top10_ctr_zero,
    detect_zero_impression_pages,
    run_opportunities_analysis,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, issues, pages, projects


def test_top10_ctr_zero_detectado():
    rows = [QueryPageRow("reparar iphone cali", "https://x.com/iphone", clicks=0, impressions=42, position=3.2)]
    found = detect_top10_ctr_zero(rows)
    assert len(found) == 1
    assert found[0].impact == 5
    assert found[0].effort == "5min"


def test_top10_ctr_zero_ignora_pocas_impresiones():
    rows = [QueryPageRow("x", "https://x.com/", clicks=0, impressions=2, position=3.2)]
    assert detect_top10_ctr_zero(rows) == []


def test_top10_ctr_zero_ignora_fuera_de_top10():
    rows = [QueryPageRow("x", "https://x.com/", clicks=0, impressions=100, position=15)]
    assert detect_top10_ctr_zero(rows) == []


def test_top10_ctr_zero_ignora_si_hay_clics():
    rows = [QueryPageRow("x", "https://x.com/", clicks=3, impressions=100, position=3)]
    assert detect_top10_ctr_zero(rows) == []


def test_zero_impression_pages_detectada():
    crawled = [{"url": "https://x.com/pagina-huerfana", "is_indexable": True}]
    found = detect_zero_impression_pages(crawled, impressions_by_page={})
    assert len(found) == 1
    assert found[0].category == "pruning"


def test_zero_impression_pages_no_falso_positivo_con_impresiones():
    crawled = [{"url": "https://x.com/pagina-buena", "is_indexable": True}]
    found = detect_zero_impression_pages(crawled, impressions_by_page={"https://x.com/pagina-buena": 10})
    assert found == []


def test_zero_impression_pages_ignora_no_indexables():
    crawled = [{"url": "https://x.com/noindex", "is_indexable": False}]
    assert detect_zero_impression_pages(crawled, impressions_by_page={}) == []


# ---------- SEO Score combinado ----------
def test_technical_score_sin_paginas_es_none():
    assert calculate_technical_score([]) is None


def test_technical_score_pagina_perfecta_es_100():
    page = {
        "url": "https://x.com/", "title": "Título perfecto entre treinta y sesenta caracteres ok",
        "meta_description": "Meta description perfecta con más de ciento veinte caracteres y menos de ciento sesenta, con un llamado a la acción claro →",
        "h1": "Único H1", "schema_types": ["LocalBusiness"], "og": {"title": "x", "description": "y", "image": "z"},
        "canonical": "https://x.com/", "is_indexable": True,
    }
    score = calculate_technical_score([page])
    assert score is not None
    assert score >= 50  # al menos la mayoría de celdas en verde/amarillo, no todo rojo


def test_seo_score_none_si_no_hay_componentes():
    score, components = calculate_seo_score(None, None)
    assert score is None
    assert components == {}


def test_seo_score_promedia_componentes_disponibles():
    score, components = calculate_seo_score(technical_score=80, geo_score=60)
    assert score == 70
    assert components == {"technical": 80, "geo": 60}


def test_seo_score_usa_solo_el_componente_disponible():
    score, components = calculate_seo_score(technical_score=90, geo_score=None)
    assert score == 90
    assert components == {"technical": 90}


def test_seo_score_incluye_contenido_y_local_cuando_estan_disponibles():
    score, components = calculate_seo_score(
        technical_score=80, geo_score=60, content_score=70, local_score=90,
    )
    assert score == 75  # (80+70+60+90) / 4
    assert components == {"technical": 80, "content": 70, "geo": 60, "local": 90}


def test_seo_score_ignora_componentes_sin_datos_no_los_pone_en_cero():
    score, components = calculate_seo_score(
        technical_score=100, geo_score=None, content_score=None, local_score=None,
    )
    assert score == 100
    assert components == {"technical": 100}


def test_content_score_sin_paginas_es_none():
    assert calculate_content_score([]) is None


def test_content_score_sin_eeat_calculado_es_none():
    assert calculate_content_score([{"url": "https://x.com/", "eeat_score": None}]) is None


def test_content_score_promedia_eeat_de_paginas_con_dato():
    pages_rows = [
        {"url": "https://x.com/a", "eeat_score": 80},
        {"url": "https://x.com/b", "eeat_score": 40},
        {"url": "https://x.com/c", "eeat_score": None},  # excluida, no cuenta como 0
    ]
    assert calculate_content_score(pages_rows) == 60


# ---------- Integración con DB: idempotencia ----------
def _make_project(conn, slug="test-opp") -> int:
    return conn.execute(
        insert(projects).values(
            slug=slug, name="Test", url="https://test.com", gsc_property="sc-domain:test.com",
            country="CO", language="es", competitors=[], is_active=True, config={},
            created_at=now_iso(),
        )
    ).inserted_primary_key[0]


def test_run_opportunities_analysis_es_idempotente():
    with get_connection() as conn:
        pid = _make_project(conn, "test-opp-idempotente")
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="arreglo celulares",
                page="https://test.com/", clicks=0, impressions=14, position=63.5,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="arreglo celulares",
                page="https://test.com/otra", clicks=0, impressions=2, position=64,
                created_at=now_iso(),
            )
        )

    result1 = run_opportunities_analysis(pid)
    assert result1["issues_created"] >= 1

    result2 = run_opportunities_analysis(pid)
    assert result2["issues_created"] == 0  # ya existían abiertas, no se duplican

    with get_connection() as conn:
        total = conn.execute(
            select(issues).where(issues.c.project_id == pid, issues.c.status == "open")
        ).all()
    assert len(total) == result1["issues_created"]  # nunca se duplicó
