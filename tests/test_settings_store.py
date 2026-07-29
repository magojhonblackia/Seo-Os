"""Tests del almacén de secretos configurables desde la UI (§ mejoras 2026-07-26).

Lo que importa proteger aquí: un valor guardado desde la UI debe ganar sobre
.env, pero si se borra el override debe volver a .env sin romper nada — y la
key completa nunca debe filtrarse en list_status()."""
from backend.settings_store import SECRET_FIELDS, clear_secret, get_secret, list_status, set_secret


def _cleanup(field: str) -> None:
    clear_secret(field)


def test_sin_override_usa_el_valor_de_env():
    _cleanup("serper_api_key")
    assert get_secret("serper_api_key", "valor-de-env") == "valor-de-env"


def test_override_desde_la_ui_gana_sobre_env():
    set_secret("serper_api_key", "valor-guardado-desde-ui")
    try:
        assert get_secret("serper_api_key", "valor-de-env") == "valor-guardado-desde-ui"
    finally:
        _cleanup("serper_api_key")


def test_clear_secret_vuelve_a_env():
    set_secret("pagespeed_api_key", "temporal")
    clear_secret("pagespeed_api_key")
    assert get_secret("pagespeed_api_key", "valor-de-env") == "valor-de-env"


def test_set_secret_rechaza_campo_no_registrado():
    try:
        set_secret("campo_inventado_que_no_existe", "x")
        assert False, "debió lanzar ValueError"
    except ValueError:
        pass


def test_set_secret_es_idempotente_upsert():
    """Guardar dos veces la misma key no debe duplicar filas ni fallar."""
    set_secret("indexnow_key", "primero")
    set_secret("indexnow_key", "segundo")
    try:
        assert get_secret("indexnow_key", "") == "segundo"
    finally:
        _cleanup("indexnow_key")


def test_list_status_nunca_expone_el_valor_real():
    set_secret("bing_webmaster_api_key", "sk-secreto-real-no-debe-salir")
    try:
        status = list_status()
        import json

        dump = json.dumps(status)
        assert "sk-secreto-real-no-debe-salir" not in dump
    finally:
        _cleanup("bing_webmaster_api_key")


def test_list_status_marca_source_ui_cuando_hay_override():
    set_secret("telegram_bot_token", "token-de-prueba")
    try:
        status = {s["field"]: s for s in list_status()}
        assert status["telegram_bot_token"]["source"] == "ui"
        assert status["telegram_bot_token"]["configured"] is True
    finally:
        _cleanup("telegram_bot_token")


def test_list_status_incluye_todas_las_keys_registradas():
    fields = {s["field"] for s in list_status()}
    assert fields == set(SECRET_FIELDS.keys())
