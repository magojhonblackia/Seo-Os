"""Tests de question_ideas.py con Google Autocomplete MOCKEADO — regla QA:
nunca llamar a Google real desde la suite (ya se validó manualmente en vivo
contra jc, ver README). Cubre: filtro de pregunta real, dedup, degradación
ante fallo de un seed (S3), honestidad del cruce con gsc_queries, y el bug
real de encoding (ISO-8859-1 declarado en el header pero httpx.Response.json()
asumiendo UTF-8 y reventando con tildes — verificado en vivo el 2026-07-26).
"""
import json
from unittest.mock import MagicMock, patch

from sqlalchemy import insert, select

from backend.collectors.question_ideas import (
    _fetch_suggestions,
    _looks_like_question,
    _strip_accents,
    fetch_question_ideas,
    persist_question_ideas,
    run_question_ideas_collector,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, keywords, projects

client = None  # no se usa TestClient aquí, todo es a nivel de módulo


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Question Ideas", url="https://mio.com",
                gsc_property="sc-domain:mio.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _resp(query: str, suggestions: list[str], encoding: str | None = "utf-8") -> MagicMock:
    payload = json.dumps([query, suggestions], ensure_ascii=False).encode(encoding or "utf-8")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = payload
    resp.encoding = encoding
    return resp


def _fake_client(query_map: dict[str, list[str]], fail_on: set[str] | None = None):
    import httpx

    fail_on = fail_on or set()

    def fake_get(url, params):
        q = params["q"]
        if q in fail_on:
            raise httpx.ConnectError("boom", request=MagicMock())
        return _resp(q, query_map.get(q, []))

    fake = MagicMock()
    fake.__enter__.return_value = fake
    fake.__exit__.return_value = False
    fake.get.side_effect = fake_get
    return fake


# ---------- Filtro de pregunta real ----------

def test_strip_accents_quita_tildes():
    assert _strip_accents("qué cómo dónde") == "que como donde"


def test_looks_like_question_acepta_marcador_y_palabra_semilla():
    assert _looks_like_question("por que celular se calienta mucho", "celular mojado")


def test_looks_like_question_rechaza_sin_marcador():
    assert not _looks_like_question("celular samsung a54 precio", "celular mojado")


def test_looks_like_question_rechaza_tangente_sin_palabra_de_la_semilla():
    """Bug real documentado en el módulo: 'donde'/'como' + seeds cortos traen
    ruido genérico que no tiene ninguna palabra de la keyword semilla."""
    assert not _looks_like_question("como hacer una tarea de matematicas", "celular mojado")


# ---------- Bug real de encoding (2026-07-26) ----------

def test_fetch_suggestions_decodifica_iso_8859_1_declarado_en_encoding():
    """Reproduce el bug real: Google responde charset=ISO-8859-1 para tildes
    y antes se usaba response.json() (asume UTF-8 sin mirar el header) ->
    'invalid continuation byte'. Ahora se decodifica con response.encoding."""
    payload = "por qué celular mojado".encode("iso-8859-1")
    suggestions_json = ["por que celular se calienta mucho", "por que celular no prende"]
    full = json.dumps([payload.decode("iso-8859-1"), suggestions_json], ensure_ascii=False).encode("iso-8859-1")

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = full
    resp.encoding = "iso-8859-1"

    fake_client = MagicMock()
    fake_client.get.return_value = resp

    result = _fetch_suggestions(fake_client, "por qué celular mojado", "co", "es")
    assert result == suggestions_json


def test_fetch_suggestions_cae_a_latin1_si_utf8_falla_y_no_hay_encoding():
    """Defensivo: si el header no trae charset (encoding=None) y el default
    UTF-8 revienta, no debe propagar la excepción — cae a latin-1."""
    full = b'["qu\xe9 tal", ["algo"]]'  # 0xe9 no es UTF-8 válido en esa posición

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = full
    resp.encoding = None

    fake_client = MagicMock()
    fake_client.get.return_value = resp

    result = _fetch_suggestions(fake_client, "qué tal", "co", "es")
    assert result == ["algo"]


# ---------- fetch_question_ideas: agrupación, filtro, degradación ----------

def test_fetch_question_ideas_filtra_y_agrupa_por_seed():
    query_map = {
        "por qué celular mojado": ["por que celular se calienta mucho", "celular samsung precio"],
        "qué hacer si celular mojado": ["que hacer si celular cae al agua"],
    }
    fake = _fake_client(query_map)

    with patch("backend.collectors.question_ideas.httpx.Client", return_value=fake), \
         patch("backend.collectors.question_ideas.time.sleep"):
        result = fetch_question_ideas(["celular mojado"], gl="co", hl="es")

    assert "celular mojado" in result
    assert "por que celular se calienta mucho" in result["celular mojado"]
    assert "que hacer si celular cae al agua" in result["celular mojado"]
    assert "celular samsung precio" not in result["celular mojado"]  # sin marcador de pregunta


def test_fetch_question_ideas_no_lanza_si_un_seed_falla_sigue_con_el_resto():
    """Regla S3: un seed que falla no debe tumbar el resto de la corrida."""
    query_map = {"por qué bateria iphone": ["por que la bateria se calienta"]}
    fake = _fake_client(query_map, fail_on={"por qué celular mojado"})

    with patch("backend.collectors.question_ideas.httpx.Client", return_value=fake), \
         patch("backend.collectors.question_ideas.time.sleep"):
        result = fetch_question_ideas(["celular mojado", "bateria iphone"], gl="co", hl="es")

    assert "celular mojado" not in result  # falló, se saltó
    assert "bateria iphone" in result  # el resto sigue funcionando


def test_fetch_question_ideas_sin_resultados_relevantes_seed_queda_ausente():
    query_map = {"por qué xyz": ["ruido genérico sin relación"]}
    fake = _fake_client(query_map)

    with patch("backend.collectors.question_ideas.httpx.Client", return_value=fake), \
         patch("backend.collectors.question_ideas.time.sleep"):
        result = fetch_question_ideas(["xyz"], gl="co", hl="es")

    assert result == {}  # S3: se reporta ausencia, no se inventa nada


# ---------- Honestidad: cruce con gsc_queries ----------

def test_persist_question_ideas_marca_already_has_real_data_si_coincide_con_gsc():
    pid = _make_project("test-question-ideas-honestidad")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, query="celular mojado cali", date="2026-07-01",
                clicks=1, impressions=10, ctr=0.1, position=3.0, created_at=now_iso(),
            )
        )

    ideas = {"celular mojado": ["que hacer si celular mojado cali no prende", "como secar celular"]}
    persist_question_ideas(pid, ideas)

    with get_connection() as conn:
        rows = conn.execute(
            select(keywords.c.keyword, keywords.c.trend_data)
            .where(keywords.c.project_id == pid, keywords.c.source == "question_ideas")
        ).all()

    by_kw = {r.keyword: r.trend_data for r in rows}
    assert by_kw["que hacer si celular mojado cali no prende"]["already_has_real_data"] is True
    assert by_kw["como secar celular"]["already_has_real_data"] is False


# ---------- run_question_ideas_collector: sin seeds ----------

def test_run_question_ideas_collector_sin_seeds_da_skipped():
    _make_project("test-question-ideas-sin-seeds")
    result = run_question_ideas_collector("test-question-ideas-sin-seeds", seed_keywords=[])
    assert result["status"] == "skipped"
