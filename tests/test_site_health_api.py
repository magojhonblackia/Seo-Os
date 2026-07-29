"""Tests de los endpoints y collectors nuevos (cobertura, GA4, sitemap):
degradación con gracia cuando falta la fuente, y forma de la respuesta."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Cobertura Test", url="https://cobertura-test.com",
                gsc_property="sc-domain:cobertura-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


# ---------- /site-health ----------
def test_site_health_sin_analisis_declara_no_disponible():
    _make_project("cov-sin-analisis")
    resp = client.get("/api/dashboard/cov-sin-analisis/site-health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "auditoría" in body["empty_reason"].lower()


def test_site_health_devuelve_el_ultimo_snapshot():
    pid = _make_project("cov-con-analisis")
    raw = {
        "coverage": {"counts": {"sitemap": 10, "crawled": 5, "indexed": 3, "inspected": 4}, "orphans": ["u"], "broken": []},
        "internal_links": {"weak": [], "deep": [], "per_page": []},
        "duplicates": {"titles": [], "metas": [], "h1s": [], "thin": []},
    }
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="site_health", status="ok",
                started_at=now_iso(), finished_at=now_iso(), raw_data=raw, created_at=now_iso(),
            )
        )
    body = client.get("/api/dashboard/cov-con-analisis/site-health").json()
    assert body["available"] is True
    assert body["coverage"]["counts"]["sitemap"] == 10
    assert body["coverage"]["orphans"] == ["u"]


# ---------- /ga4 ----------
def test_ga4_sin_configurar_declara_motivo():
    _make_project("cov-ga4-vacio")
    body = client.get("/api/dashboard/cov-ga4-vacio/ga4").json()
    assert body["available"] is False
    assert "GA4" in body["empty_reason"]


def test_ga4_con_datos_calcula_totales():
    pid = _make_project("cov-ga4-ok")
    raw = {
        "property_id": "123", "days": 28, "conversion_metric": "keyEvents",
        "rows": [
            {"landing_page": "/a", "sessions": 100, "users": 90, "conversions": 5.0, "conversion_rate": 0.05, "conversion_metric": "keyEvents"},
            {"landing_page": "/b", "sessions": 50, "users": 45, "conversions": 1.0, "conversion_rate": 0.02, "conversion_metric": "keyEvents"},
        ],
    }
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="ga4", status="ok",
                started_at=now_iso(), finished_at=now_iso(), raw_data=raw, created_at=now_iso(),
            )
        )
    body = client.get("/api/dashboard/cov-ga4-ok/ga4").json()
    assert body["available"] is True
    assert body["totals"] == {"sessions": 150, "conversions": 6.0}
    assert body["conversion_metric"] == "keyEvents"


def test_ga4_collector_sin_property_id_hace_skip_no_falla():
    """Regla S3: sin GA4_PROPERTY_ID el collector no revienta la auditoría."""
    _make_project("cov-ga4-skip")
    with patch("backend.collectors.ga4.settings") as mock_settings:
        mock_settings.ga4_property_id = ""
        resp = client.post("/api/collect/ga4/cov-ga4-skip", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------- sitemap ----------
def test_sitemap_collector_sin_sitemap_hace_skip():
    _make_project("cov-sitemap-skip")
    with patch("backend.collectors.sitemap.fetch_sitemap_urls", return_value=([], ["x"], ["404"])):
        resp = client.post("/api/collect/sitemap/cov-sitemap-skip", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_sitemap_parser_lee_urlset_y_sitemapindex():
    from backend.collectors.sitemap import _parse_sitemap

    urlset = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x.com/a</loc></url><url><loc>https://x.com/b</loc></url></urlset>"""
    urls, subs = _parse_sitemap(urlset)
    assert urls == ["https://x.com/a", "https://x.com/b"]
    assert subs == []

    index = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x.com/sm1.xml</loc></sitemap></sitemapindex>"""
    urls2, subs2 = _parse_sitemap(index)
    assert urls2 == []
    assert subs2 == ["https://x.com/sm1.xml"]


def test_site_health_sin_crawler_hace_skip():
    pid = _make_project("cov-sh-sin-crawler")
    resp = client.post("/api/collect/site_health/cov-sh-sin-crawler", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------- Regresión real 2026-07-24: redirect debe SOBREVIVIR entre crawls ----------
def test_redirect_persistido_evita_falso_duplicado_aunque_no_se_re_crawlee():
    """Bug real: /reparacion-iphone-buenaventura (301 -> /reparacion-iphone/
    buenaventura) se crawleó UNA vez y quedó persistido en `pages` con
    redirected_to. Un crawl POSTERIOR de 100 páginas ya no vuelve a visitarla
    (nadie la enlaza) — su fila en `pages` sigue viva con el título viejo.
    Antes del fix, site_health solo miraba el redirect_map del ÚLTIMO crawl y
    la volvía a marcar como 'título duplicado'. Debe seguir excluida."""
    from backend.analyzers.site_health import run_site_health_analysis
    from backend.db.schema import pages

    pid = _make_project("cov-redirect-persistido")
    with get_connection() as conn:
        # fila vieja: SÍ tiene redirected_to (se crawleó hace tiempo)
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://cobertura-test.com/vieja",
                redirected_to="https://cobertura-test.com/nueva",
                title="Mismo título", status_code=200, is_indexable=True,
                first_seen=now_iso(), last_crawled=now_iso(),
            )
        )
        # página real vigente, mismo título (es lo que sirve el redirect)
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://cobertura-test.com/nueva",
                title="Mismo título", status_code=200, is_indexable=True,
                first_seen=now_iso(), last_crawled=now_iso(),
            )
        )
        # el crawl MÁS RECIENTE solo visitó la nueva — no vuelve a ver la vieja
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="crawler", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages": [{"url": "https://cobertura-test.com/nueva", "status_code": 200}], "errors": []},
                created_at=now_iso(),
            )
        )

    result = run_site_health_analysis(pid)
    assert result["duplicate_titles"] == 0


# ---------- Contradicción robots.txt vs sitemap (§ mejoras 2026-07-25) ----------
def test_robots_bloquea_url_del_sitemap_genera_issue():
    """El sitemap declara 'indexa esto' y robots.txt bloquea la misma URL para
    el crawler — mensaje contradictorio real. _load_robots hace una petición
    real a robots.txt, así que se mockea para no depender de la red en tests."""
    from unittest.mock import MagicMock, patch

    from sqlalchemy import select

    from backend.analyzers.site_health import run_site_health_analysis

    pid = _make_project("cov-robots-conflict")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="crawler", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages": [{"url": "https://cobertura-test.com/", "status_code": 200}], "errors": []},
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="sitemap", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"urls": ["https://cobertura-test.com/", "https://cobertura-test.com/bloqueada"]},
                created_at=now_iso(),
            )
        )

    fake_robots = MagicMock()
    fake_robots.can_fetch.side_effect = lambda ua, url: "bloqueada" not in url

    with patch("backend.collectors.crawler._load_robots", return_value=fake_robots):
        result = run_site_health_analysis(pid)

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(
                snapshots.c.project_id == pid, snapshots.c.collector == "site_health"
            ).order_by(snapshots.c.id.desc()).limit(1)
        ).first()
    conflicts = row[0]["coverage"]["robots_sitemap_conflicts"]
    assert conflicts == ["https://cobertura-test.com/bloqueada"]
    assert result["status"] == "ok"


def test_robots_txt_ilegible_no_rompe_site_health():
    """S3: si robots.txt no se puede leer, el análisis sigue sin ese chequeo,
    nunca se cae toda la auditoría por esto."""
    from unittest.mock import patch

    from backend.analyzers.site_health import run_site_health_analysis

    pid = _make_project("cov-robots-error")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="crawler", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages": [{"url": "https://cobertura-test.com/", "status_code": 200}], "errors": []},
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="sitemap", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"urls": ["https://cobertura-test.com/"]},
                created_at=now_iso(),
            )
        )

    with patch("backend.collectors.crawler._load_robots", side_effect=RuntimeError("timeout")):
        result = run_site_health_analysis(pid)

    assert result["status"] == "ok"
