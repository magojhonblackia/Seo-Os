"""Tests de trends.py con pytrends MOCKEADO — regla QA: nunca llamar a Google
Trends real desde la suite (ya se validó manualmente contra datos reales, ver
README). Cubre: chunking en batches de 5, fallback CO-VAC -> CO, degradación
con gracia ante error de red.
"""
from unittest.mock import MagicMock, patch

import pandas as pd

from backend.collectors.trends import PRIMARY_GEO, FALLBACK_GEO, _chunk, fetch_trends


def test_chunk_agrupa_de_a_5():
    items = [f"kw{i}" for i in range(12)]
    chunks = _chunk(items, 5)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [5, 5, 2]


def _fake_df(keyword: str, values: list[int]) -> pd.DataFrame:
    dates = pd.date_range("2026-04-01", periods=len(values), freq="D")
    return pd.DataFrame({keyword: values}, index=dates)


def test_fetch_trends_caso_feliz_geo_primario():
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.interest_over_time.return_value = _fake_df("reparar iphone cali", [10, 20, 30])

        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_trends(["reparar iphone cali"])

    assert "reparar iphone cali" in result
    assert result["reparar iphone cali"]["geo_used"] == PRIMARY_GEO


def test_fetch_trends_fallback_a_geo_pais_si_vacio():
    """Reproduce el pitfall real verificado: CO-VAC vacío -> reintenta con CO."""
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.interest_over_time.side_effect = [
            pd.DataFrame(),  # CO-VAC: vacío (pitfall real)
            _fake_df("reparar iphone", [50, 60, 70]),  # CO: con datos
        ]

        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_trends(["reparar iphone"])

    assert "reparar iphone" in result
    assert result["reparar iphone"]["geo_used"] == FALLBACK_GEO


def test_fetch_trends_sin_datos_ni_en_fallback_queda_ausente():
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.interest_over_time.side_effect = [pd.DataFrame(), pd.DataFrame()]

        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_trends(["keyword rarisima sin ningun volumen"])

    assert result == {}  # S3: se reporta ausencia, no se inventa un cero


def test_fetch_trends_error_de_red_no_lanza_excepcion():
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.interest_over_time.side_effect = Exception("Google Trends bloqueó la IP")

        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_trends(["x"])  # no debe lanzar, debe degradarse (S3)

    assert result == {}


def test_fetch_trends_respeta_batch_size_5():
    keywords_list = [f"kw{i}" for i in range(7)]
    calls = []

    def fake_build_payload(batch, geo, timeframe):
        calls.append((tuple(batch), geo))

    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.build_payload.side_effect = fake_build_payload
        instance.interest_over_time.return_value = pd.DataFrame()  # todo vacío, no importa aquí

        with patch("backend.collectors.trends.time.sleep"):
            fetch_trends(keywords_list)

    # 2 batches primarios (5+2) + hasta 2 batches de fallback (5+2) = 4 llamadas máx
    primary_calls = [c for c in calls if c[1] == PRIMARY_GEO]
    assert len(primary_calls[0][0]) <= 5


# ---------- Retry con backoff ante 429 (§ mejoras 2026-07-26) ----------

def test_reintenta_ante_429_y_recupera():
    """Bug real: antes un solo 429 mataba el batch completo, cero reintentos.
    Ahora debe reintentar y recuperarse si el segundo intento sí funciona."""
    from backend.collectors.trends import _call_with_retry

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("response code 429: Too Many Requests")
        return "ok"

    with patch("backend.collectors.trends.time.sleep") as mock_sleep:
        result = _call_with_retry(flaky)

    assert result == "ok"
    assert calls["n"] == 3  # falló 2 veces, funcionó a la 3ra
    assert mock_sleep.call_count == 2  # un sleep por reintento


def test_no_reintenta_errores_que_no_son_429():
    """Un error que no es rate-limit no debe reintentarse aquí — ya lo
    maneja el fallback de geo existente, no hay que duplicar la lógica."""
    from backend.collectors.trends import _call_with_retry

    calls = {"n": 0}

    def siempre_falla_feo():
        calls["n"] += 1
        raise ValueError("respuesta corrupta, formato inesperado")

    with patch("backend.collectors.trends.time.sleep") as mock_sleep:
        try:
            _call_with_retry(siempre_falla_feo)
            assert False, "debió relanzar la excepción"
        except ValueError:
            pass

    assert calls["n"] == 1  # ni un solo reintento
    mock_sleep.assert_not_called()


def test_agota_reintentos_y_relanza_el_429():
    from backend.collectors.trends import MAX_RETRIES_ON_429, _call_with_retry

    calls = {"n": 0}

    def siempre_429():
        calls["n"] += 1
        raise Exception("429 Too Many Requests")

    with patch("backend.collectors.trends.time.sleep"):
        try:
            _call_with_retry(siempre_429)
            assert False, "debió relanzar tras agotar reintentos"
        except Exception as exc:
            assert "429" in str(exc)

    assert calls["n"] == MAX_RETRIES_ON_429 + 1  # intento inicial + reintentos


def test_backoff_es_exponencial_con_jitter():
    """No debe ser un delay fijo: cada reintento espera más que el anterior,
    y el jitter evita que reintentos en paralelo se sincronicen."""
    from backend.collectors.trends import _call_with_retry

    def siempre_429():
        raise Exception("429")

    with patch("backend.collectors.trends.time.sleep") as mock_sleep:
        try:
            _call_with_retry(siempre_429)
        except Exception:
            pass

    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert delays == sorted(delays)  # estrictamente creciente
    assert len(set(delays)) == len(delays)  # ningún delay repetido exacto (jitter)
