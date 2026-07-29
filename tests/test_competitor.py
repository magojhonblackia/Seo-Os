"""Tests del collector de competidores. Sin llamadas de red reales — se probó
manualmente contra capriservicios.com (inalcanzable) y myphonedoctor.co (403),
ambos casos reales que motivaron las reglas de honestidad de datos abajo."""
from unittest.mock import patch

import pytest
from sqlalchemy import insert

from backend.collectors.competitor import _aggregate_content_insights, scan_competitor
from backend.collectors.crawler import CrawledPage
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects


def _make_project(slug: str, competitors: list[str]) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Competitor", url="https://test-owner.com",
                gsc_property="sc-domain:test-owner.com", country="CO", language="es",
                competitors=competitors, is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def test_competidor_no_registrado_lanza_error():
    _make_project("test-comp-no-registrado", competitors=["otro.com"])
    with pytest.raises(ValueError, match="no está registrado"):
        scan_competitor("test-comp-no-registrado", "no-registrado.com")


def test_sitio_inalcanzable_reporta_scores_none():
    """Caso real: capriservicios.com — conexión falla por completo, robots.txt
    bloquea todo por defecto (comportamiento real de RobotFileParser), 0 páginas."""
    _make_project("test-comp-inalcanzable", competitors=["inalcanzable.com"])

    with patch("backend.collectors.competitor.crawl_site", return_value=([], [])), \
         patch("backend.collectors.competitor.is_reachable", return_value=False):
        result = scan_competitor("test-comp-inalcanzable", "inalcanzable.com")

    assert result["pages_crawled"] == 0
    assert result["technical_score"] is None
    assert result["geo_score"] is None
    assert result["reachable"] is False
    assert "inalcanzable" in result["note"].lower()


def test_sitio_bloquea_robots_con_403_no_inventa_geo_score():
    """Caso real: myphonedoctor.co — 403 en robots.txt. Antes del fix, esto
    se interpretaba como 'sin restricciones' (score falso de 70); ahora debe
    quedar en None con nota explicando el porqué."""
    _make_project("test-comp-403", competitors=["bloqueado403.com"])

    fake_page = CrawledPage(
        url="https://bloqueado403.com/", status_code=403, title=None,
        h1_tags=[], schema_types=[], og={}, is_indexable=True,
    )

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = ""

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return FakeResponse(403)

    with patch("backend.collectors.competitor.crawl_site", return_value=([fake_page], [])), \
         patch("backend.collectors.competitor.is_reachable", return_value=True), \
         patch("backend.collectors.competitor.httpx") as mock_httpx:
        mock_httpx.Client.return_value = FakeClient()
        mock_httpx.HTTPError = Exception
        result = scan_competitor("test-comp-403", "bloqueado403.com")

    assert result["reachable"] is True
    assert result["geo_score"] is None
    assert "403" in result["note"] or "bloqueó" in result["note"]


def test_pagina_de_bloqueo_waf_no_cuenta_como_contenido_real():
    """Caso real detectado en myphonedoctor.co: el 403 devolvía una página de
    challenge de Cloudflare ('Checking your browser...') con su propio title/H1,
    que se colaba como si fuera contenido real del competidor en sample_keywords
    y en el technical_score. Debe excluirse cualquier página con status != 200."""
    waf_block_page = CrawledPage(
        url="https://bloqueado403.com/",
        status_code=403,
        title="Checking your browser before accessing bloqueado403.com",
        h1_tags=["Just a moment..."],
        schema_types=[],
        og={},
        is_indexable=True,
    )
    _make_project("test-comp-waf", competitors=["bloqueado403-waf.com"])

    with patch("backend.collectors.competitor.crawl_site", return_value=([waf_block_page], [])), \
         patch("backend.collectors.competitor.is_reachable", return_value=False):
        result = scan_competitor("test-comp-waf", "bloqueado403-waf.com")

    assert result["technical_score"] is None
    assert result["sample_keywords_count"] == 0


def test_scan_exitoso_calcula_technical_score():
    fake_page = CrawledPage(
        url="https://bueno.com/",
        status_code=200,
        title="Título perfecto entre treinta y sesenta caracteres",
        meta_description="Meta description con más de ciento veinte caracteres y menos de ciento sesenta, con CTA →",
        h1_tags=["H1 único"],
        schema_types=["LocalBusiness"],
        og={"title": "x", "description": "y", "image": "z"},
        canonical="https://bueno.com/",
        is_indexable=True,
    )
    _make_project("test-comp-bueno", competitors=["bueno.com"])

    with patch("backend.collectors.competitor.crawl_site", return_value=([fake_page], [])), \
         patch("backend.collectors.competitor.is_reachable", return_value=False):
        result = scan_competitor("test-comp-bueno", "bueno.com", max_pages=5)

    assert result["pages_crawled"] == 1
    assert result["technical_score"] is not None
    assert result["technical_score"] >= 50


# ---------- _aggregate_content_insights (§ máxima información del competidor) ----------

def test_aggregate_content_insights_sin_paginas_no_inventa_nada():
    result = _aggregate_content_insights([])
    assert result["schema_coverage"] == {}
    assert result["avg_word_count"] is None
    assert result["eeat_signals"] == {}
    assert result["local_business_detected"] is None


def test_aggregate_content_insights_calcula_schema_coverage_y_eeat():
    pages_ = [
        CrawledPage(
            url="https://x.com/a", status_code=200, schema_types=["LocalBusiness", "FAQPage"],
            word_count=500, title="Título de diez", meta_description="Meta corta",
            has_author=True, has_date=True, has_contact=False,
        ),
        CrawledPage(
            url="https://x.com/b", status_code=200, schema_types=["LocalBusiness"],
            word_count=300, title="Otro título", meta_description=None,
            has_author=False, has_date=True, has_contact=False,
        ),
    ]
    result = _aggregate_content_insights(pages_)
    assert result["schema_coverage"] == {"LocalBusiness": 2, "FAQPage": 1}
    assert result["avg_word_count"] == 400.0
    assert result["eeat_signals"] == {"has_author_pct": 50, "has_date_pct": 100, "has_contact_pct": 0}
    assert result["avg_meta_length"] == len("Meta corta")  # solo cuenta la página que sí tiene meta


def test_aggregate_content_insights_detecta_local_business_de_la_primera_pagina_que_lo_tenga():
    pages_ = [
        CrawledPage(url="https://x.com/a", status_code=200, local_business_schema=None),
        CrawledPage(
            url="https://x.com/b", status_code=200,
            local_business_schema={"name": "Competidor SAS", "telephone": "+57 300 000 0000"},
        ),
    ]
    result = _aggregate_content_insights(pages_)
    assert result["local_business_detected"] == {"name": "Competidor SAS", "telephone": "+57 300 000 0000"}