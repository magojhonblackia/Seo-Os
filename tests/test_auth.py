"""Tests de auth opcional por Bearer token (§4.2, S8 Fase 4).

Regla: sin AUTH_TOKEN configurado (el caso normal, uso local en 127.0.0.1),
todo sigue funcionando sin pedir ningún header — eso ya lo confirma el resto
de la suite (280+ tests que nunca mandan Authorization y pasan). Aquí se
prueba específicamente el comportamiento CUANDO sí hay un token configurado."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.api.auth import require_auth_if_configured


def test_sin_token_configurado_no_exige_nada():
    with patch("backend.api.auth.settings") as mock_settings:
        mock_settings.has_auth_token = False
        # No debe lanzar sin importar qué traiga (o no traiga) el header
        require_auth_if_configured(authorization=None)
        require_auth_if_configured(authorization="cualquier cosa")


def test_con_token_configurado_rechaza_sin_header():
    with patch("backend.api.auth.settings") as mock_settings:
        mock_settings.has_auth_token = True
        mock_settings.auth_token = "secreto123"
        with pytest.raises(HTTPException) as exc_info:
            require_auth_if_configured(authorization=None)
        assert exc_info.value.status_code == 401


def test_con_token_configurado_rechaza_token_incorrecto():
    with patch("backend.api.auth.settings") as mock_settings:
        mock_settings.has_auth_token = True
        mock_settings.auth_token = "secreto123"
        with pytest.raises(HTTPException) as exc_info:
            require_auth_if_configured(authorization="Bearer token-equivocado")
        assert exc_info.value.status_code == 401


def test_con_token_configurado_acepta_token_correcto():
    with patch("backend.api.auth.settings") as mock_settings:
        mock_settings.has_auth_token = True
        mock_settings.auth_token = "secreto123"
        require_auth_if_configured(authorization="Bearer secreto123")  # no debe lanzar


def test_con_token_configurado_rechaza_sin_prefijo_bearer():
    with patch("backend.api.auth.settings") as mock_settings:
        mock_settings.has_auth_token = True
        mock_settings.auth_token = "secreto123"
        with pytest.raises(HTTPException):
            require_auth_if_configured(authorization="secreto123")
