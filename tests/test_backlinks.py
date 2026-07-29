"""Tests del módulo Backlinks (Fase 4): analyzer (anchors/tóxicos/disavow),
collector (Bing Webmaster, mockeado — sin llamadas de red reales) y API
(degradación elegante cuando no hay credenciales, ver .env.example).

Nota: Moz se integró aquí en una versión anterior (Domain/Page Authority
reales vía Mozscape) pero se removió el 2026-07-18 — la credencial disponible
resultó ser de un producto que el propio Moz marcó como deprecado
(`x-accessid: DEPRECATED` en la respuesta real de su API). Bing Webmaster
queda como única fuente activa de backlinks."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.analyzers.backlinks import (
    BacklinkRow,
    build_backlinks_issues,
    calculate_anchor_distribution,
    detect_toxic_backlinks,
    generate_disavow_file,
)
from backend.collectors.backlinks import run_backlinks_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.main import app

# ---------- Analyzer ----------

def _row(anchor="reparacion de iphone", domain="referente.com"):
    return BacklinkRow(
        source_url=f"https://{domain}/pagina",
        source_domain=domain,
        target_url="https://miproyecto.com/",
        anchor_text=anchor,
        source="bing",
        domain_authority=None,  # ninguna fuente activa lo reporta hoy
        spam_score=None,
    )


def test_anchor_distribution_calcula_porcentajes():
    rows = [_row("reparacion iphone"), _row("reparacion iphone"), _row("marca propia")]
    dist = calculate_anchor_distribution(rows)
    top = next(d for d in dist if d["anchor_text"] == "reparacion iphone")
    assert top["count"] == 2
    assert abs(top["percentage"] - 66.7) < 0.1


def test_anchor_distribution_marca_sobre_optimizacion():
    # 4 de 5 backlinks (80%) con el mismo anchor "money keyword" -> riesgo real
    rows = [_row("reparacion iphone cali") for _ in range(4)] + [_row("otro anchor")]
    dist = calculate_anchor_distribution(rows)
    top = next(d for d in dist if d["anchor_text"] == "reparacion iphone cali")
    assert top["over_optimized"] is True


def test_anchor_distribution_vacia_sin_backlinks():
    assert calculate_anchor_distribution([]) == []


def test_anchor_sin_texto_se_agrupa_como_sin_texto():
    rows = [_row(anchor="")]
    dist = calculate_anchor_distribution(rows)
    assert dist[0]["anchor_text"] == "(sin texto / imagen)"


def test_toxico_por_tld_heuristica():
    """Única señal de toxicidad disponible hoy: TLD de la lista de spam
    conocida — marcada explícitamente como heurística débil, no certeza."""
    rows = [_row(domain="oferta-barata.xyz")]
    toxic = detect_toxic_backlinks(rows)
    assert len(toxic) == 1
    assert "heurística" in toxic[0]["reason"]


def test_no_toxico_tld_normal():
    rows = [_row(domain="sitio-normal.com")]
    assert detect_toxic_backlinks(rows) == []


def test_disavow_file_formato_google():
    toxic = [{"source_domain": "spammy.com", "source_url": "https://spammy.com/x", "anchor_text": "a", "reason": "TLD .xyz asociado a spam"}]
    content = generate_disavow_file(toxic)
    assert "domain:spammy.com" in content
    assert "search.google.com/search-console/disavow-links" in content


def test_disavow_file_deduplica_por_dominio():
    toxic = [
        {"source_domain": "spammy.com", "source_url": "https://spammy.com/a", "anchor_text": "a", "reason": "r"},
        {"source_domain": "spammy.com", "source_url": "https://spammy.com/b", "anchor_text": "b", "reason": "r"},
    ]
    content = generate_disavow_file(toxic)
    assert content.count("domain:spammy.com") == 1


def test_disavow_file_vacio_sin_toxicos():
    content = generate_disavow_file([])
    assert "domain:" not in content


def test_build_issues_vacio_sin_toxicos():
    assert build_backlinks_issues(total_count=50, toxic_count=0) == []


def test_build_issues_critical_cuando_ratio_alto():
    issues = build_backlinks_issues(total_count=10, toxic_count=5)  # 50%
    assert issues[0].severity == "critical"


def test_build_issues_medium_cuando_ratio_bajo():
    issues = build_backlinks_issues(total_count=100, toxic_count=1)  # 1%
    assert issues[0].severity == "medium"


# ---------- Collector (mockeado, sin red real) ----------

def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Backlinks", url="https://test-backlinks.com",
                gsc_property="sc-domain:test-backlinks.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def test_collector_sin_credenciales_devuelve_skipped():
    """Degradación elegante (S3): sin BING_WEBMASTER_API_KEY, el collector no
    falla ni inventa datos — devuelve skipped con motivo claro."""
    _make_project("test-bl-sin-config")
    with patch("backend.collectors.backlinks.settings") as mock_settings:
        mock_settings.has_bing_webmaster = False
        result = run_backlinks_collector("test-bl-sin-config")

    assert result["status"] == "skipped"
    assert result["summary"] is None
    assert "sin configurar" in result["message"].lower()


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeBingClient:
    """Mockea la forma REAL de respuesta de GetUrlLinks, confirmada contra la
    documentación oficial de Microsoft Learn (IWebmasterApi.GetUrlLinks) tras
    un 400 Bad Request real con una key de producción — no es una suposición."""

    def __init__(self):
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        self.last_params = params
        return _FakeResponse(
            {
                "d": {
                    "Details": [
                        {"AnchorText": "reparacion de celulares cali", "Url": "https://directorio-local.com/negocios"},
                    ],
                    "TotalPages": 1,
                }
            }
        )


def test_collector_bing_persiste_backlinks_reales():
    _make_project("test-bl-bing")
    fake_client = _FakeBingClient()
    with patch("backend.collectors.backlinks.settings") as mock_settings, \
         patch("backend.collectors.backlinks.httpx") as mock_httpx:
        mock_settings.has_bing_webmaster = True
        mock_settings.bing_webmaster_api_key = "fake_bing_key"
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception

        result = run_backlinks_collector("test-bl-bing")

    assert result["status"] == "ok"
    assert result["summary"]["total_backlinks"] == 1
    assert "bing" in result["summary"]["sources_used"]
    # Regresión del bug real: el parámetro es "link" (no "url"), y "page" es obligatorio.
    assert "link" in fake_client.last_params
    assert "url" not in fake_client.last_params
    assert fake_client.last_params["page"] == 0
    assert fake_client.last_params["link"].startswith('"') and fake_client.last_params["link"].endswith('"')


def test_collector_persiste_idempotente_sin_duplicar():
    """Correr el collector dos veces el mismo backlink no debe duplicar filas
    (regla S5, upsert por project_id+source_url+target_url+anchor_text)."""
    _make_project("test-bl-idempotente")
    with patch("backend.collectors.backlinks.settings") as mock_settings, \
         patch("backend.collectors.backlinks.httpx") as mock_httpx:
        mock_settings.has_bing_webmaster = True
        mock_settings.bing_webmaster_api_key = "fake_bing_key"
        mock_httpx.Client.return_value = _FakeBingClient()
        mock_httpx.HTTPError = Exception

        run_backlinks_collector("test-bl-idempotente")
        result = run_backlinks_collector("test-bl-idempotente")

    from sqlalchemy import select

    from backend.db.schema import backlinks as backlinks_table, projects as projects_table

    with get_connection() as conn:
        pid = conn.execute(
            select(projects_table.c.id).where(projects_table.c.slug == "test-bl-idempotente")
        ).scalar()
        count = conn.execute(
            select(backlinks_table).where(backlinks_table.c.project_id == pid)
        ).all()

    assert len(count) == 1  # no 2, aunque el collector corrió dos veces
    assert result["status"] == "ok"


class _FakeErrorClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        raise RuntimeError("timeout simulado")


def test_collector_bing_error_no_rompe_la_app():
    """Regla S3: un fallo de red en Bing se traduce en status=error, nunca en
    una excepción que tumbe el resto de la app."""
    _make_project("test-bl-error")
    with patch("backend.collectors.backlinks.settings") as mock_settings, \
         patch("backend.collectors.backlinks.httpx") as mock_httpx:
        mock_settings.has_bing_webmaster = True
        mock_settings.bing_webmaster_api_key = "fake_bing_key"
        mock_httpx.Client.return_value = _FakeErrorClient()
        mock_httpx.HTTPError = Exception

        result = run_backlinks_collector("test-bl-error")

    assert result["status"] == "error"


# ---------- API ----------

client = TestClient(app)


def test_api_backlinks_sin_datos_muestra_empty_reason():
    _make_project("test-bl-api-vacio")
    resp = client.get("/api/dashboard/test-bl-api-vacio/backlinks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["backlinks"] == []
    assert "empty_reason" in body
    assert "BING_WEBMASTER_API_KEY" in body["empty_reason"] or "Sin backlinks" in body["empty_reason"]


def test_api_disavow_txt_vacio_sin_datos():
    _make_project("test-bl-api-disavow-vacio")
    resp = client.get("/api/dashboard/test-bl-api-disavow-vacio/disavow.txt")
    assert resp.status_code == 200
    assert "domain:" not in resp.text


def test_api_backlinks_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/backlinks")
    assert resp.status_code == 404


def test_api_collect_backlinks_module_registrado():
    _make_project("test-bl-api-collect")
    with patch("backend.collectors.backlinks.settings") as mock_settings:
        mock_settings.has_bing_webmaster = False
        resp = client.post("/api/collect/backlinks/test-bl-api-collect", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


# ---------- Link reclaim (§ herramientas de mercado 2026-07-24) ----------
def test_reclaim_detecta_backlink_a_pagina_rota():
    from backend.analyzers.backlinks import find_reclaim_opportunities

    bl = _row(anchor="reparaciones", domain="referente.com")
    bl = bl.__class__(**{**bl.__dict__, "target_url": "https://miproyecto.com/vieja-rota"})
    opps = find_reclaim_opportunities([bl], redirect_map={}, broken_targets={"https://miproyecto.com/vieja-rota"})
    assert len(opps) == 1
    assert opps[0]["issue"] == "broken"


def test_reclaim_detecta_backlink_a_url_que_redirige():
    from backend.analyzers.backlinks import find_reclaim_opportunities

    bl = _row(anchor="reparaciones", domain="referente.com")
    bl = bl.__class__(**{**bl.__dict__, "target_url": "https://miproyecto.com/vieja"})
    rmap = {"miproyecto.com/vieja": "miproyecto.com/nueva"}
    opps = find_reclaim_opportunities([bl], redirect_map=rmap, broken_targets=set())
    assert len(opps) == 1
    assert opps[0]["issue"] == "redirects"
    assert opps[0]["final_url"] == "miproyecto.com/nueva"


def test_reclaim_no_marca_backlink_a_url_final_correcta():
    from backend.analyzers.backlinks import find_reclaim_opportunities

    bl = _row(anchor="reparaciones", domain="referente.com")
    bl = bl.__class__(**{**bl.__dict__, "target_url": "https://miproyecto.com/vigente"})
    opps = find_reclaim_opportunities([bl], redirect_map={}, broken_targets=set())
    assert opps == []


def test_build_reclaim_issues_separa_roto_de_redirect():
    from backend.analyzers.backlinks import build_reclaim_issues

    issues = build_reclaim_issues([
        {"source_url": "a", "source_domain": "a.com", "target_url": "x", "anchor_text": "t", "issue": "broken", "final_url": None},
        {"source_url": "b", "source_domain": "b.com", "target_url": "y", "anchor_text": "t", "issue": "redirects", "final_url": "z"},
    ])
    severities = {i.severity for i in issues}
    assert "high" in severities and "medium" in severities
    assert all(i.category == "backlinks" for i in issues)
