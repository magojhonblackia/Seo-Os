"""Tests de SEO Local (Fase 4): NAP consistency, cobertura de schema
LocalBusiness, y el pipeline completo run_local_analysis sobre un snapshot
real del crawler — sin llamadas de red (regla QA)."""
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from backend.analyzers.local_seo import (
    analyze_local_business_schema,
    analyze_nap_consistency,
    build_local_issues,
    calculate_local_score,
    extract_phones_from_text,
    run_local_analysis,
)
from backend.collectors.crawler import _extract_local_business_schema, _extract_page_data
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, scores, snapshots
from backend.main import app

client = TestClient(app)


# ---------- Crawler: extracción de schema LocalBusiness ----------

def test_extrae_local_business_con_telefono():
    from bs4 import BeautifulSoup

    html = """
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Mi Negocio", "telephone": "+57 300 123 4567"}
    </script>
    """
    soup = BeautifulSoup(html, "lxml")
    result = _extract_local_business_schema(soup)
    assert result == {"name": "Mi Negocio", "telephone": "+57 300 123 4567"}


def test_sin_local_business_devuelve_none():
    from bs4 import BeautifulSoup

    html = '<script type="application/ld+json">{"@type": "Organization", "name": "X"}</script>'
    soup = BeautifulSoup(html, "lxml")
    assert _extract_local_business_schema(soup) is None


def test_extract_page_data_incluye_local_business_schema():
    html = (
        '<html><head><title>T</title>'
        '<script type="application/ld+json">'
        '{"@type": "LocalBusiness", "name": "JC", "telephone": "3001234567"}'
        "</script></head><body><h1>H</h1></body></html>"
    )
    page = _extract_page_data("https://x.com/", 200, html)
    assert page.local_business_schema == {"name": "JC", "telephone": "3001234567"}


# ---------- Analyzer: extracción y normalización de teléfonos ----------

def test_extract_phones_encuentra_multiples_formatos():
    text = "Llámanos al 300 123 4567 o al (301) 987-6543. WhatsApp: +57 302 111 2222"
    phones = extract_phones_from_text(text)
    assert len(phones) == 3


def test_extract_phones_vacio_sin_texto():
    assert extract_phones_from_text("") == []
    assert extract_phones_from_text("sin numeros aca") == []


# ---------- Analyzer: consistencia NAP ----------

def test_nap_consistente_mismo_telefono_todas_paginas():
    pages = [
        {"url": "https://x.com/", "body_text": "Llámanos: 300 123 4567", "schema_phone": None},
        {"url": "https://x.com/contacto", "body_text": "Tel: 3001234567", "schema_phone": None},
    ]
    result = analyze_nap_consistency(pages)
    assert result["is_consistent"] is True
    assert len(result["phones"]) == 1
    assert result["phones"][0]["pages_count"] == 2


def test_nap_inconsistente_telefonos_distintos():
    pages = [
        {"url": "https://x.com/", "body_text": "Tel: 300 123 4567", "schema_phone": None},
        {"url": "https://x.com/contacto", "body_text": "Tel: 302 999 8888", "schema_phone": None},
    ]
    result = analyze_nap_consistency(pages)
    assert result["is_consistent"] is False
    assert len(result["phones"]) == 2


def test_nap_prioriza_telefono_de_schema():
    pages = [{"url": "https://x.com/", "body_text": "", "schema_phone": "300 123 4567"}]
    result = analyze_nap_consistency(pages)
    assert result["phones"][0]["from_schema"] is True


def test_nap_sin_telefonos_detectados():
    pages = [{"url": "https://x.com/", "body_text": "Bienvenido a nuestro sitio", "schema_phone": None}]
    result = analyze_nap_consistency(pages)
    assert result["phones"] == []
    assert result["primary_phone"] is None


def test_nap_vacio_sin_paginas():
    result = analyze_nap_consistency([])
    assert result["is_consistent"] is True
    assert result["phones"] == []


# ---------- Analyzer: cobertura de schema ----------

def test_schema_coverage_calcula_ratio():
    pages = [
        {"url": "a", "has_local_business_schema": True},
        {"url": "b", "has_local_business_schema": False},
    ]
    result = analyze_local_business_schema(pages)
    assert result["coverage_ratio"] == 0.5
    assert result["has_any"] is True


def test_schema_coverage_sin_ninguna():
    pages = [{"url": "a", "has_local_business_schema": False}]
    result = analyze_local_business_schema(pages)
    assert result["has_any"] is False
    assert result["coverage_ratio"] == 0.0


# ---------- Local score ----------

def test_local_score_perfecto():
    nap = {"phones": [{"phone_normalized": "1", "raw_examples": ["1"], "pages_count": 2, "from_schema": True}], "is_consistent": True, "primary_phone": "1"}
    schema = {"has_any": True, "coverage_ratio": 1.0, "pages_with_schema": ["a"]}
    assert calculate_local_score(nap, schema) == 100


def test_local_score_penaliza_inconsistencia():
    nap = {"phones": [{"pages_count": 1, "from_schema": False, "raw_examples": ["1"]}, {"pages_count": 1, "from_schema": False, "raw_examples": ["2"]}], "is_consistent": False, "primary_phone": None}
    schema = {"has_any": True, "coverage_ratio": 1.0, "pages_with_schema": ["a"]}
    assert calculate_local_score(nap, schema) == 60


def test_local_score_penaliza_sin_telefono_y_sin_schema():
    nap = {"phones": [], "is_consistent": True, "primary_phone": None}
    schema = {"has_any": False, "coverage_ratio": 0.0, "pages_with_schema": []}
    assert calculate_local_score(nap, schema) == 40  # 100 - 30 (sin telefono) - 30 (sin schema)


def test_local_score_nunca_negativo():
    nap = {"phones": [{"pages_count": 1, "from_schema": False, "raw_examples": ["1"]}, {"pages_count": 1, "from_schema": False, "raw_examples": ["2"]}], "is_consistent": False, "primary_phone": None}
    schema = {"has_any": False, "coverage_ratio": 0.0, "pages_with_schema": []}
    assert calculate_local_score(nap, schema) >= 0


# ---------- Issues Formato Mago ----------

def test_issue_inconsistencia_es_high():
    nap = {"phones": [{"pages_count": 1, "from_schema": False, "raw_examples": ["300 123 4567"]}, {"pages_count": 1, "from_schema": False, "raw_examples": ["302 999 8888"]}], "is_consistent": False, "primary_phone": None}
    schema = {"has_any": True, "coverage_ratio": 1.0, "pages_with_schema": ["a"]}
    issues = build_local_issues(nap, schema)
    assert any(i.severity == "high" and "Inconsistencia" in i.title for i in issues)


def test_issue_sin_telefono_es_medium():
    nap = {"phones": [], "is_consistent": True, "primary_phone": None}
    schema = {"has_any": True, "coverage_ratio": 1.0, "pages_with_schema": ["a"]}
    issues = build_local_issues(nap, schema)
    assert any(i.severity == "medium" and "teléfono" in i.title for i in issues)


def test_issue_sin_schema_es_high():
    nap = {"phones": [{"pages_count": 1, "from_schema": True, "raw_examples": ["1"]}], "is_consistent": True, "primary_phone": "1"}
    schema = {"has_any": False, "coverage_ratio": 0.0, "pages_with_schema": []}
    issues = build_local_issues(nap, schema)
    assert any(i.severity == "high" and "LocalBusiness" in i.title for i in issues)


def test_sin_issues_cuando_todo_esta_bien():
    nap = {"phones": [{"pages_count": 2, "from_schema": True, "raw_examples": ["1"]}], "is_consistent": True, "primary_phone": "1"}
    schema = {"has_any": True, "coverage_ratio": 1.0, "pages_with_schema": ["a"]}
    assert build_local_issues(nap, schema) == []


# ---------- run_local_analysis: pipeline completo ----------

def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Local", url="https://test-local.com",
                gsc_property="sc-domain:test-local.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _insert_crawler_snapshot(project_id: int, pages_raw: list[dict]) -> None:
    now = now_iso()
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project_id, collector="crawler", status="ok",
                started_at=now, finished_at=now_iso(),
                raw_data={"pages": pages_raw, "errors": []}, created_at=now_iso(),
            )
        )


def test_run_local_analysis_sin_crawler_devuelve_skipped():
    pid = _make_project("test-local-sin-crawler")
    result = run_local_analysis(pid)
    assert result["status"] == "skipped"
    assert result["issues_created"] == 0


def test_run_local_analysis_detecta_inconsistencia_real():
    pid = _make_project("test-local-inconsistente")
    _insert_crawler_snapshot(pid, [
        {"url": "https://test-local.com/", "body_text_sample": "Llámanos: 300 123 4567", "local_business_schema": None},
        {"url": "https://test-local.com/contacto", "body_text_sample": "Tel: 302 999 8888", "local_business_schema": None},
    ])

    result = run_local_analysis(pid)

    assert result["status"] == "ok"
    assert result["pages_analyzed"] == 2
    assert result["local_score"] < 100
    assert result["issues_created"] >= 1


def test_run_local_analysis_persiste_score_local():
    pid = _make_project("test-local-score-persistido")
    _insert_crawler_snapshot(pid, [
        {"url": "https://test-local.com/", "body_text_sample": "", "local_business_schema": {"name": "X", "telephone": "3001234567"}},
    ])
    run_local_analysis(pid)

    with get_connection() as conn:
        row = conn.execute(
            select(scores.c.value, scores.c.kind).where(scores.c.project_id == pid, scores.c.kind == "local")
        ).first()
    assert row is not None
    assert row.value == 100  # 1 telefono consistente (de schema) + schema presente


def test_run_local_analysis_idempotente_no_duplica_score():
    pid = _make_project("test-local-idempotente")
    _insert_crawler_snapshot(pid, [
        {"url": "https://test-local.com/", "body_text_sample": "300 123 4567", "local_business_schema": None},
    ])
    run_local_analysis(pid)
    run_local_analysis(pid)

    with get_connection() as conn:
        rows = conn.execute(
            select(scores).where(scores.c.project_id == pid, scores.c.kind == "local")
        ).all()
    assert len(rows) == 1


def test_run_local_analysis_no_duplica_issues_en_segunda_corrida():
    pid = _make_project("test-local-no-duplica-issues")
    _insert_crawler_snapshot(pid, [
        {"url": "https://test-local.com/", "body_text_sample": "300 123 4567", "local_business_schema": None},
        {"url": "https://test-local.com/2", "body_text_sample": "302 999 8888", "local_business_schema": None},
    ])
    first = run_local_analysis(pid)
    second = run_local_analysis(pid)
    assert first["issues_created"] >= 1
    assert second["issues_created"] == 0  # ya estaban abiertas, no se duplican (regla S5)


# ---------- API ----------

def test_api_local_sin_datos_muestra_empty_reason():
    _make_project("test-local-api-vacio")
    resp = client.get("/api/dashboard/test-local-api-vacio/local")
    assert resp.status_code == 200
    assert resp.json()["empty_reason"]


def test_api_local_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/local")
    assert resp.status_code == 404


def test_api_local_devuelve_score_tras_analisis():
    pid = _make_project("test-local-api-con-datos")
    _insert_crawler_snapshot(pid, [
        {"url": "https://test-local.com/", "body_text_sample": "300 123 4567", "local_business_schema": {"name": "X", "telephone": "300 123 4567"}},
    ])
    client.post("/api/collect/local/test-local-api-con-datos", json={})

    resp = client.get("/api/dashboard/test-local-api-con-datos/local")
    body = resp.json()
    assert body["score"] == 100
    assert body["nap"]["is_consistent"] is True
    assert body["schema"]["has_any"] is True


def test_api_collect_local_module_registrado_sin_crawler():
    _make_project("test-local-api-collect-vacio")
    resp = client.post("/api/collect/local/test-local-api-collect-vacio", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
