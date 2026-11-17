import json
import logging
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app


_SITEVERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
_TIMEOUT_SEGUNDOS = 10
_RESPUESTA_MAX_BYTES = 16 * 1024
_LOGGER = logging.getLogger(__name__)


class RecaptchaConfiguracionError(RuntimeError):
    pass


class RecaptchaNoDisponibleError(RuntimeError):
    pass


def verificar_token_recaptcha(token):
    configuracion = current_app.config
    if not configuracion.get("RECAPTCHA_ENABLED", False):
        if current_app.testing or configuracion.get("RECAPTCHA_ALLOW_DISABLED", False):
            return True
        _LOGGER.error(
            "reCAPTCHA está deshabilitado fuera de pruebas o desarrollo; se rechazan reportes."
        )
        raise RecaptchaConfiguracionError

    clave_sitio = configuracion.get("RECAPTCHA_SITE_KEY")
    clave_secreta = configuracion.get("RECAPTCHA_SECRET_KEY")
    hosts_permitidos = _hosts_permitidos(
        configuracion.get("RECAPTCHA_ALLOWED_HOSTNAMES", ())
    )
    if not clave_sitio or not clave_secreta or not hosts_permitidos:
        _LOGGER.error("Configuración de reCAPTCHA incompleta; se rechazan reportes.")
        raise RecaptchaConfiguracionError

    if not isinstance(token, str) or not token.strip():
        return False

    datos = urlencode(
        {
            "secret": clave_secreta,
            "response": token,
        }
    ).encode("ascii")
    solicitud = Request(
        _SITEVERIFY_URL,
        data=datos,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(solicitud, timeout=_TIMEOUT_SEGUNDOS) as respuesta:
            resultado = json.loads(respuesta.read(_RESPUESTA_MAX_BYTES))
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        _LOGGER.warning("No fue posible verificar reCAPTCHA con Google.")
        raise RecaptchaNoDisponibleError from None

    if not isinstance(resultado, dict):
        _LOGGER.warning("Google devolvió una respuesta reCAPTCHA no válida.")
        raise RecaptchaNoDisponibleError
    if resultado.get("success") is not True:
        return False

    hostname = resultado.get("hostname")
    return (
        isinstance(hostname, str)
        and _normalizar_hostname(hostname) in hosts_permitidos
    )


def _hosts_permitidos(valor):
    if isinstance(valor, str):
        valores = valor.split(",")
    elif isinstance(valor, (tuple, list, set)):
        valores = valor
    else:
        valores = ()

    return {
        hostname
        for elemento in valores
        if isinstance(elemento, str)
        and (hostname := _normalizar_hostname(elemento))
    }


def _normalizar_hostname(hostname):
    return hostname.strip().lower().rstrip("./")
