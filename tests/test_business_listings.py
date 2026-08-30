"""Tests de business_listings.py (§ mejoras 2026-08-29): citas locales /
listados de negocio vía DataForSEO Business Listings API. Sin llamadas de
red reales (regla QA) — el caso real usado como fixture (jc, "JC
Reparaciones") es exactamente el que se verificó en vivo antes de escribir
este módulo: un listado propio (dominio jcreparaciones.com), y un segundo
listado real distinto en la misma ciudad, sin teléfono ni dominio, que el
módulo debe marcar como "posible duplicado" — ver docstring del módulo real."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.analyzers.mago import MagoIssue
from backend.collectors.business_listings import (
    _domain_of,
    _fetch_business_listings,
    _normalize_phone,
    _normalize_title,
    build_business_listing_issues,
    classify_listings,
    run_business_listings_check,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import issues, projects, scores, snapshots


def _make_project(slug: str, name: str = "JC Reparaciones", url: str = "https://jcreparaciones-test.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name=name, url=url,
                gsc_property=f"sc-domain:{url.split('//')[-1]}", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.last_body = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.last_body = json
        return self._response


# ---------- Helpers de normalización ----------
def test_normalize_phone_toma_los_ultimos_10_digitos():
    assert _normalize_phone("+57 316 333 3400") == "3163333400"


def test_normalize_phone_none_devuelve_vacio():
    assert _normalize_phone(None) == ""


def test_normalize_title_quita_acentos_y_puntuacion():
    assert _normalize_title("JC REPARACIONES | Servicio Técnico Apple y Multimarca | Cali") == \
        _normalize_title("jc reparaciones servicio tecnico apple y multimarca cali")


def test_domain_of_extrae_dominio_de_url():
    assert _domain_of("https://www.jcreparaciones.com/sobre-nosotros") == "jcreparaciones.com"


def test_domain_of_dominio_bare_se_queda_igual():
    assert _domain_of("jcreparaciones.com") == "jcreparaciones.com"


# ---------- classify_listings — caso real verificado en vivo contra jc ----------
def _real_items() -> list[dict]:
    return [
        {
            "title": "JC REPARACIONES | Servicio Técnico Apple y Multimarca | Cali",
            "address": "Av. 5a Nte. #20-13 local 103, San Vicente, Cali, Valle del Cauca",
            "phone": "+573163333400",
            "domain": "jcreparaciones.com",
            "url": "https://jcreparaciones.com/",
            "category": "Mobile phone repair shop",
            "is_claimed": True,
        },
        {
            "title": "Jc Reparaciones",
            "address": "Cra. 103 #13 L, Ciudad Jardín, Cali, Valle del Cauca",
            "phone": None,
            "domain": None,
            "url": None,
            "category": None,
            "is_claimed": False,
        },
        {
            "title": "JC aguilar reparaciones",  # nombre distinto, otro país -> ruido, se ignora
            "address": "Veracruz, Ver., México",
            "phone": "+522293665675",
            "domain": "www.facebook.com",
            "url": "https://www.facebook.com/profile.php?id=1",
            "category": "Mobile phone repair shop",
            "is_claimed": True,
        },
    ]


def test_classify_listings_encuentra_el_propio_por_dominio():
    result = classify_listings(_real_items(), "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    assert result["own_listing"]["domain"] == "jcreparaciones.com"


def test_classify_listings_marca_posible_duplicado_caso_real():
    """Caso real de jc: mismo nombre normalizado, misma ciudad, SIN teléfono
    ni dominio que lo confirme como cita real."""
    result = classify_listings(_real_items(), "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    assert len(result["possible_duplicates"]) == 1
    assert "Ciudad Jardín" in result["possible_duplicates"][0]["address"]


def test_classify_listings_descarta_nombre_distinto_como_ruido():
    result = classify_listings(_real_items(), "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    all_titles = [result["own_listing"]["title"]] + [d["title"] for d in result["possible_duplicates"]] + [c["title"] for c in result["citations"]]
    assert "JC aguilar reparaciones" not in all_titles


def test_classify_listings_mismo_nombre_otro_pais_no_es_duplicado():
    """Regresión de un bug real encontrado en la verificación en vivo contra
    jc: "JC Reparaciones" es un nombre genérico repetido en México,
    Argentina, Paraguay, etc. Sin el filtro de ubicación, la primera versión
    marcó 12 negocios de otros países como "posible duplicado" — puro ruido
    de nombre repetido, no una señal real. Mismo título normalizado, SIN
    ningún token de ciudad/región en común con el listado propio -> debe
    descartarse, no reportarse."""
    items = [
        {
            "title": "Jc Reparaciones", "address": "Pedro Juan Caballero 130103",  # Paraguay, sin ciudad reconocible en común
            "phone": None, "domain": None, "url": None, "category": None, "is_claimed": False,
        },
        {
            "title": "JC Reparaciones", "address": "Calle Juan Escutia, Bo. Nuevo, 82245 Sin.",  # Sinaloa, México
            "phone": None, "domain": None, "url": None, "category": None, "is_claimed": False,
        },
        _real_items()[0],  # el listado propio real, para que haya ancla de ubicación
    ]
    result = classify_listings(items, "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    assert result["possible_duplicates"] == []


def test_classify_listings_sin_listado_propio_no_marca_ningun_duplicado():
    """Sin encontrar el dominio propio en los resultados, no hay ancla de
    ubicación confiable — mejor no reportar duplicados que arriesgar ruido
    global (mismo criterio P1 que el resto del proyecto)."""
    items = [
        {"title": "JC Reparaciones", "address": "Cualquier ciudad", "phone": None, "domain": None, "url": None, "category": None, "is_claimed": False},
    ]
    result = classify_listings(items, "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    assert result["own_listing"] is None
    assert result["possible_duplicates"] == []


def test_classify_listings_detecta_cita_real_por_telefono():
    items = [
        {
            "title": "JC Reparaciones - Directorio Cali", "address": "Cali", "phone": "+573163333400",
            "domain": "directoriocali.com", "url": "https://directoriocali.com/jc", "category": None, "is_claimed": False,
        }
    ]
    result = classify_listings(items, "https://jcreparaciones.com", "3163333400", "JC Reparaciones")
    assert len(result["citations"]) == 1
    assert result["citations"][0]["domain"] == "directoriocali.com"


def test_classify_listings_sin_telefono_propio_no_rompe():
    """Si el análisis local (local_seo.py) todavía no corrió, own_phone_normalized
    llega vacío — no debe reventar, solo no puede clasificar por teléfono."""
    result = classify_listings(_real_items(), "https://jcreparaciones.com", "", "JC Reparaciones")
    assert result["own_listing"] is not None  # por dominio sigue funcionando


# ---------- build_business_listing_issues ----------
def test_build_issues_una_por_duplicado_posible():
    classification = {
        "own_listing": None, "citations": [],
        "possible_duplicates": [{"title": "Jc Reparaciones", "address": "Otra dirección", "is_claimed": False}],
    }
    found = build_business_listing_issues(classification, "JC Reparaciones")
    assert len(found) == 1
    assert found[0].severity == "medium"
    assert found[0].category == "local_citations"


def test_build_issues_vacio_sin_duplicados():
    classification = {"own_listing": None, "citations": [], "possible_duplicates": []}
    assert build_business_listing_issues(classification, "x") == []


# ---------- _fetch_business_listings ----------
def test_fetch_business_listings_body_es_array_plano():
    """Regresión del bug real de esta sesión: el body correcto es un array
    plano, NO {"tasks": [...]}."""
    response = {"tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": _real_items()}]}]}
    fake_client = _FakeClient(_FakeResponse(response))
    with patch("backend.collectors.business_listings.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        items, error = _fetch_business_listings("login", "password", "JC Reparaciones")

    assert error is None
    assert len(items) == 3
    assert isinstance(fake_client.last_body, list)
    assert fake_client.last_body[0]["title"] == "JC Reparaciones"


def test_fetch_business_listings_status_code_error_no_rompe():
    response = {"tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    with patch("backend.collectors.business_listings.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(response))
        mock_httpx.HTTPError = Exception
        items, error = _fetch_business_listings("login", "password", "x")

    assert items == []
    assert "Invalid Field" in error


# ---------- run_business_listings_check (integración) ----------
def test_run_sin_credenciales_da_skipped():
    _make_project("test-bl-listings-sin-config")
    with patch("backend.collectors.business_listings.get_secret", return_value=None):
        result = run_business_listings_check("test-bl-listings-sin-config")
    assert result["status"] == "skipped"


def test_run_persiste_snapshot_y_genera_issue_de_duplicado():
    pid = _make_project("test-bl-listings-persiste", url="https://jcreparaciones.com")
    response = {"tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": _real_items()}]}]}
    with patch("backend.collectors.business_listings.get_secret", return_value="fake"), \
         patch("backend.collectors.business_listings.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(response))
        mock_httpx.HTTPError = Exception
        result = run_business_listings_check("test-bl-listings-persiste")

    assert result["status"] == "ok"
    assert result["summary"]["own_listing_found"] is True
    assert result["summary"]["possible_duplicates_found"] == 1

    with get_connection() as conn:
        snap = conn.execute(
            select(snapshots.c.collector, snapshots.c.status).where(snapshots.c.id == result["snapshot_id"])
        ).first()
        issue_rows = conn.execute(
            select(issues).where(issues.c.project_id == pid, issues.c.category == "local_citations", issues.c.status == "open")
        ).all()
    assert snap.collector == "business_listings"
    assert len(issue_rows) == 1


def test_run_usa_nap_del_ultimo_analisis_local_si_existe():
    """Si local_seo.py ya corrió, el teléfono propio (scores.breakdown.nap.
    primary_phone) se usa para clasificar citas reales por teléfono."""
    pid = _make_project("test-bl-listings-con-nap")
    with get_connection() as conn:
        conn.execute(
            insert(scores).values(
                project_id=pid, date="2026-08-29", kind="local", value=80,
                breakdown={"nap": {"primary_phone": "3163333400"}},
            )
        )

    response_items = [
        {
            "title": "JC Reparaciones - Directorio", "address": "Cali", "phone": "+573163333400",
            "domain": "otrodirectorio.com", "url": "https://otrodirectorio.com/jc", "category": None, "is_claimed": False,
        }
    ]
    response = {"tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": response_items}]}]}
    with patch("backend.collectors.business_listings.get_secret", return_value="fake"), \
         patch("backend.collectors.business_listings.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(response))
        mock_httpx.HTTPError = Exception
        result = run_business_listings_check("test-bl-listings-con-nap")

    assert result["summary"]["citations_found"] == 1
