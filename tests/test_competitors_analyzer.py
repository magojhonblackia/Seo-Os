"""Tests de competitors.py: matriz competitiva + keyword gap honesto."""
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.analyzers.competitors import (
    build_competitive_matrix,
    build_competitor_comparison,
    get_keyword_gap_for_project,
    keyword_gap,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, pages, projects, snapshots
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, competitors: list[str]) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Matrix", url="https://mio.com",
                gsc_property="sc-domain:mio.com", country="CO", language="es",
                competitors=competitors, is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


# ---------- keyword_gap (pura) ----------
def test_keyword_gap_detecta_tema_sin_overlap():
    own_queries = ["reparar iphone cali", "arreglo de celulares"]
    competitor_topics = ["Reparación de MacBook en Cali | Servicio Técnico"]
    gaps = keyword_gap(own_queries, competitor_topics)
    assert len(gaps) == 1
    assert "macbook" not in " ".join(own_queries)  # confirma que es un tema nuevo
    assert "NO es un ranking real" in gaps[0]["note"]


def test_keyword_gap_no_detecta_si_hay_overlap():
    own_queries = ["reparacion de macbook cali"]
    competitor_topics = ["Reparación de MacBook en Cali"]
    gaps = keyword_gap(own_queries, competitor_topics)
    assert gaps == []


def test_keyword_gap_ignora_duplicados():
    own_queries = []
    competitor_topics = ["Mismo Tema", "mismo tema", "MISMO TEMA"]
    gaps = keyword_gap(own_queries, competitor_topics)
    assert len(gaps) == 1


def test_keyword_gap_ignora_topics_vacios():
    assert keyword_gap(["x"], ["", "   "]) == []


# ---------- build_competitive_matrix (con DB) ----------
def test_matriz_incluye_propio_sitio_y_competidor_sin_escanear():
    pid = _make_project("test-matrix-sin-escanear", competitors=["competidor-nuevo.com"])
    with get_connection() as conn:
        matrix = build_competitive_matrix(conn, pid)

    assert len(matrix) == 2
    own_row = next(r for r in matrix if r["is_own_site"])
    comp_row = next(r for r in matrix if not r["is_own_site"])
    assert own_row["domain"] == "mio.com"
    assert comp_row["domain"] == "competidor-nuevo.com"
    assert comp_row["technical_score"] is None
    assert "Sin escanear" in comp_row["note"]


def test_matriz_incluye_datos_reales_de_snapshot_de_competidor():
    pid = _make_project("test-matrix-con-snapshot", competitors=["escaneado.com"])
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:escaneado.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "pages_crawled": 12, "technical_score": 65, "geo_score": 40,
                    "sample_keywords": ["x"],
                },
                created_at=now_iso(),
            )
        )
        matrix = build_competitive_matrix(conn, pid)

    comp_row = next(r for r in matrix if not r["is_own_site"])
    assert comp_row["pages_crawled"] == 12
    assert comp_row["technical_score"] == 65
    assert comp_row["geo_score"] == 40


# ---------- get_keyword_gap_for_project (integración) ----------
def test_get_keyword_gap_for_project_sin_snapshot_devuelve_vacio():
    pid = _make_project("test-gap-sin-snapshot", competitors=["x.com"])
    with get_connection() as conn:
        gaps = get_keyword_gap_for_project(conn, pid, "x.com")
    assert gaps == []


def test_get_keyword_gap_for_project_usa_queries_reales():
    pid = _make_project("test-gap-real", competitors=["competidor.com"])
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali",
                page="https://mio.com/", clicks=1, impressions=10, ctr=0.1, position=5,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:competidor.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"sample_keywords": ["Reparación de MacBook Pro en Cali"]},
                created_at=now_iso(),
            )
        )
        gaps = get_keyword_gap_for_project(conn, pid, "competidor.com")

    assert len(gaps) == 1
    assert "macbook" in gaps[0]["competitor_topic"].lower()


# ---------- build_competitor_comparison (§ máxima información del competidor) ----------

def test_comparison_none_si_competidor_nunca_fue_escaneado():
    pid = _make_project("test-comparison-sin-escanear", competitors=["nunca.com"])
    with get_connection() as conn:
        comparison = build_competitor_comparison(conn, pid, "nunca.com")
    assert comparison is None


def test_comparison_incluye_nuestras_metricas_y_las_del_competidor():
    pid = _make_project("test-comparison-completa", competitors=["rival.com"])
    with get_connection() as conn:
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://mio.com/", first_seen=now_iso(),
                schema_types=["LocalBusiness"], word_count=400, title="Nuestro título",
                meta_description="Nuestra meta", has_author=True, has_date=False, has_contact=True,
            )
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:rival.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "pages_crawled": 8, "technical_score": 70, "geo_score": 50,
                    "schema_coverage": {"LocalBusiness": 8, "FAQPage": 3},
                    "avg_word_count": 650.0, "avg_title_length": 55.0, "avg_meta_length": 145.0,
                    "eeat_signals": {"has_author_pct": 90, "has_date_pct": 80, "has_contact_pct": 100},
                    "local_business_detected": {"name": "Rival SAS", "telephone": "+57 300 111 2222"},
                    "note": None,
                },
                created_at=now_iso(),
            )
        )
        comparison = build_competitor_comparison(conn, pid, "rival.com")

    assert comparison is not None
    assert comparison["own"]["schema_coverage"] == {"LocalBusiness": 1}
    assert comparison["own"]["avg_word_count"] == 400.0
    assert comparison["competitor"]["local_business_detected"]["name"] == "Rival SAS"
    # FAQPage: el competidor lo usa, nosotros no — el gap concreto que se busca
    assert comparison["schema_gap"] == ["FAQPage"]


def test_comparison_sin_datos_propios_no_inventa_valores():
    pid = _make_project("test-comparison-sin-datos-propios", competitors=["rival2.com"])
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:rival2.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages_crawled": 3, "technical_score": 60},
                created_at=now_iso(),
            )
        )
        comparison = build_competitor_comparison(conn, pid, "rival2.com")

    assert comparison["own"]["avg_word_count"] is None  # sin páginas propias crawleadas
    assert comparison["own"]["schema_coverage"] == {}


# ---------- GET /api/dashboard/{slug}/competitors/{domain}/detail ----------

def test_api_competitor_detail_sin_escanear_declara_no_disponible():
    _make_project("test-api-compdetail-vacio", competitors=["rival.com"])
    resp = client.get("/api/dashboard/test-api-compdetail-vacio/competitors/rival.com/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "reason" in body


def test_api_competitor_detail_con_datos_reales():
    pid = _make_project("test-api-compdetail-ok", competitors=["rival.com"])
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:rival.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "pages_crawled": 5, "technical_score": 75, "geo_score": 55,
                    "schema_coverage": {"Review": 2},
                    "avg_word_count": 500.0, "avg_title_length": 45.0, "avg_meta_length": 140.0,
                    "eeat_signals": {"has_author_pct": 60, "has_date_pct": 40, "has_contact_pct": 80},
                    "local_business_detected": None, "note": None,
                },
                created_at=now_iso(),
            )
        )
    resp = client.get("/api/dashboard/test-api-compdetail-ok/competitors/rival.com/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["schema_gap"] == ["Review"]


def test_api_competitor_detail_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/competitors/rival.com/detail")
    assert resp.status_code == 404
