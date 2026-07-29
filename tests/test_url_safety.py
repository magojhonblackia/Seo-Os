"""Tests del guard SSRF: DNS mockeado para determinismo, sin depender de la
red real ni de que un dominio de prueba siga existiendo."""
from unittest.mock import patch

import pytest

from backend.analyzers.url_safety import UnsafeURLError, ensure_scheme, validate_public_url


def _fake_addrinfo(ip: str):
    return [(2, 1, 6, "", (ip, 0))]


# ---------- ensure_scheme: bug real reportado por el usuario ----------
# El usuario escribió una URL sin "https://" (ej. "ejemplo.com") y recibió
# "422: Esquema no permitido: ''" — un error confuso en vez de autocompletar.
def test_ensure_scheme_agrega_https_si_falta():
    assert ensure_scheme("ejemplo.com") == "https://ejemplo.com"
    assert ensure_scheme("ejemplo.com/pagina") == "https://ejemplo.com/pagina"
    assert ensure_scheme("www.ejemplo.com") == "https://www.ejemplo.com"


def test_ensure_scheme_no_toca_esquema_ya_presente():
    assert ensure_scheme("http://ejemplo.com") == "http://ejemplo.com"
    assert ensure_scheme("https://ejemplo.com") == "https://ejemplo.com"
    assert ensure_scheme("ftp://ejemplo.com") == "ftp://ejemplo.com"


def test_ensure_scheme_quita_espacios():
    assert ensure_scheme("  ejemplo.com  ") == "https://ejemplo.com"


def test_validate_public_url_completa_esquema_faltante():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("93.184.216.34")):
        result = validate_public_url("ejemplo-sin-esquema.com")
    assert result == "https://ejemplo-sin-esquema.com"


def test_rechaza_esquema_no_http():
    with pytest.raises(UnsafeURLError, match="Esquema no permitido"):
        validate_public_url("ftp://ejemplo.com/archivo")


def test_rechaza_file_scheme():
    with pytest.raises(UnsafeURLError, match="Esquema no permitido"):
        validate_public_url("file:///etc/passwd")


def test_rechaza_localhost_explicito():
    with pytest.raises(UnsafeURLError, match="localhost"):
        validate_public_url("http://localhost:8000/api/projects")


def test_rechaza_url_sin_host():
    with pytest.raises(UnsafeURLError, match="falta el host"):
        validate_public_url("https://")


def test_rechaza_ip_loopback_directa():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("127.0.0.1")):
        with pytest.raises(UnsafeURLError, match="privada/reservada"):
            validate_public_url("http://127.0.0.1:8000/")


def test_rechaza_red_privada_10():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("10.0.0.5")):
        with pytest.raises(UnsafeURLError, match="privada/reservada"):
            validate_public_url("http://servidor-interno.local/")


def test_rechaza_red_privada_192_168():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("192.168.1.1")):
        with pytest.raises(UnsafeURLError):
            validate_public_url("http://mi-router.com/")


def test_rechaza_endpoint_metadata_nube():
    """169.254.169.254 es el endpoint de metadata de AWS/GCP/Azure — vector
    clásico de robo de credenciales vía SSRF."""
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("169.254.169.254")):
        with pytest.raises(UnsafeURLError, match="privada/reservada"):
            validate_public_url("http://metadata-disfrazado.com/latest/meta-data/")


def test_rechaza_loopback_ipv6():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("::1")):
        with pytest.raises(UnsafeURLError):
            validate_public_url("http://ipv6-local.com/")


def test_rechaza_ipv4_mapeada_en_ipv6():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("::ffff:127.0.0.1")):
        with pytest.raises(UnsafeURLError):
            validate_public_url("http://mapeado.com/")


def test_rechaza_dominio_no_resoluble():
    import socket

    with patch("backend.analyzers.url_safety.socket.getaddrinfo", side_effect=socket.gaierror("no resuelve")):
        with pytest.raises(UnsafeURLError, match="No se pudo resolver"):
            validate_public_url("http://dominio-que-no-existe-xyz123.com/")


def test_permite_dominio_publico_valido():
    with patch("backend.analyzers.url_safety.socket.getaddrinfo", return_value=_fake_addrinfo("93.184.216.34")):
        result = validate_public_url("https://ejemplo-publico.com/pagina")
    assert result == "https://ejemplo-publico.com/pagina"


def test_permite_si_alguna_ip_resuelta_es_publica_y_ninguna_privada():
    with patch(
        "backend.analyzers.url_safety.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0)), (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0))],
    ):
        result = validate_public_url("https://ejemplo-dual-stack.com/")
    assert result == "https://ejemplo-dual-stack.com/"
