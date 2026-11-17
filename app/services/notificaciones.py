import json
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app


_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT_SEGUNDOS = 10
_RESEND_ERROR_BODY_MAX_BYTES = 8192
_DIAGNOSTICO_MAX_CARACTERES = 240
_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-correlation-id", "cf-ray")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_RESEND_KEY_RE = re.compile(r"\bre_[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_REQUEST_TOKEN_RE = re.compile(r"req_[A-Za-z0-9_-]{1,80}")
_CF_RAY_RE = re.compile(r"[A-F0-9]{8,32}-[A-Z]{3}", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|authorization|token)\b"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]+",
    re.IGNORECASE,
)
_LOGGER = logging.getLogger(__name__)


class EmailAvisoNoConfiguradoError(RuntimeError):
    def __init__(
        self,
        mensaje="La configuración del correo está incompleta.",
        proveedor="unknown",
    ):
        super().__init__(mensaje)
        self.proveedor = proveedor


class EmailAvisoEnvioError(RuntimeError):
    def __init__(self, proveedor, estado_http=None):
        super().__init__("No fue posible completar el envío de la notificación.")
        self.proveedor = proveedor
        self.estado_http = estado_http


def enviar_aviso_reporte(reporte):
    configuracion = current_app.config
    proveedor = configuracion.get("EMAIL_PROVIDER", "smtp").strip().lower()

    if proveedor == "resend":
        _enviar_con_resend(reporte, configuracion)
    elif proveedor == "smtp":
        _enviar_con_smtp(reporte, configuracion)
    else:
        raise EmailAvisoNoConfiguradoError(proveedor="unknown")

    return proveedor


def _enviar_con_resend(reporte, configuracion):
    clave_api = configuracion.get("RESEND_API_KEY")
    remitente = configuracion.get("RESEND_FROM")
    destinatario = configuracion.get("EMAIL_REPORTS_TO")
    if not clave_api or not remitente or not destinatario:
        raise EmailAvisoNoConfiguradoError(proveedor="resend")

    cuerpo = _contenido_reporte(reporte)
    datos = json.dumps(
        {
            "from": remitente,
            "to": [destinatario],
            "subject": _asunto_reporte(reporte),
            "text": cuerpo,
        }
    ).encode("utf-8")
    solicitud = Request(
        _RESEND_URL,
        data=datos,
        headers={
            "Authorization": f"Bearer {clave_api}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SabanaCentroAguaComunidad/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(solicitud, timeout=_TIMEOUT_SEGUNDOS) as respuesta:
            estado_http = getattr(respuesta, "status", 200)
            if not 200 <= estado_http < 300:
                raise EmailAvisoEnvioError("resend", estado_http)
    except HTTPError as error:
        _registrar_error_http_resend(
            error,
            reporte,
            (clave_api, remitente, destinatario),
        )
        raise EmailAvisoEnvioError("resend", error.code) from None
    except (URLError, TimeoutError, OSError):
        raise EmailAvisoEnvioError("resend") from None


def _registrar_error_http_resend(error, reporte, configuracion_privada):
    try:
        contenido = error.read(_RESEND_ERROR_BODY_MAX_BYTES).decode("utf-8")
        respuesta = json.loads(contenido)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        respuesta = None

    status_code = None
    nombre = None
    mensaje = None
    if isinstance(respuesta, dict):
        valor_status = respuesta.get("statusCode")
        if (
            isinstance(valor_status, int)
            and not isinstance(valor_status, bool)
            and 100 <= valor_status <= 599
        ):
            status_code = str(valor_status)
        elif isinstance(valor_status, str) and re.fullmatch(r"[1-5]\d{2}", valor_status):
            status_code = valor_status

        valores_privados = [
            *configuracion_privada,
            _asunto_reporte(reporte),
            _contenido_reporte(reporte),
            reporte.municipio,
            reporte.barrio,
            reporte.tipo_falla,
            reporte.descripcion,
        ]
        nombre = _limpiar_diagnostico(respuesta.get("name"), valores_privados)
        mensaje = _limpiar_diagnostico(respuesta.get("message"), valores_privados)

    if respuesta is None:
        _LOGGER.warning(
            "Resend rechazó la solicitud: http_status=%s content_type=%s "
            "request_ids=%s message=non_json_response",
            error.code,
            _tipo_contenido_respuesta(error.headers),
            _identificadores_solicitud(error.headers),
        )
        return

    _LOGGER.warning(
        "Resend rechazó la solicitud: http_status=%s statusCode=%s name=%s message=%s",
        error.code,
        status_code or "unknown",
        nombre or "unknown",
        mensaje or "unavailable",
    )


def _tipo_contenido_respuesta(headers):
    if headers is None or not hasattr(headers, "get"):
        return "unknown"
    valor = headers.get("Content-Type")
    tipo = _limpiar_diagnostico(valor, ())
    return tipo[:100] if tipo else "unknown"


def _identificadores_solicitud(headers):
    if headers is None or not hasattr(headers, "get"):
        return "none"

    identificadores = []
    for nombre in _REQUEST_ID_HEADERS:
        valor = headers.get(nombre)
        if not isinstance(valor, str):
            continue
        valor = valor.strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", valor):
            continue
        if (
            _EMAIL_RE.search(valor)
            or _RESEND_KEY_RE.search(valor)
            or _BEARER_RE.search(valor)
            or _CREDENTIAL_ASSIGNMENT_RE.search(valor)
        ):
            continue
        if len(valor) >= 32 and not (
            _UUID_RE.fullmatch(valor)
            or _REQUEST_TOKEN_RE.fullmatch(valor)
            or (nombre == "cf-ray" and _CF_RAY_RE.fullmatch(valor))
        ):
            continue
        identificadores.append(f"{nombre}={valor}")

    return ",".join(identificadores) or "none"


def _limpiar_diagnostico(valor, valores_privados):
    if not isinstance(valor, str):
        return None

    texto = valor
    for valor_privado in valores_privados:
        if isinstance(valor_privado, str) and len(valor_privado) >= 3:
            texto = re.sub(
                re.escape(valor_privado),
                "[redacted]",
                texto,
                flags=re.IGNORECASE,
            )

    texto = _CREDENTIAL_ASSIGNMENT_RE.sub("[redacted credential]", texto)
    texto = _BEARER_RE.sub("[redacted token]", texto)
    texto = _RESEND_KEY_RE.sub("[redacted token]", texto)
    texto = _LONG_TOKEN_RE.sub("[redacted token]", texto)
    texto = _EMAIL_RE.sub("[redacted email]", texto)
    texto = re.sub(r"[\x00-\x1f\x7f]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:_DIAGNOSTICO_MAX_CARACTERES]


def _enviar_con_smtp(reporte, configuracion):
    servidor = configuracion.get("SMTP_SERVER")
    remitente = configuracion.get("SMTP_SENDER")
    destinatario = configuracion.get("EMAIL_REPORTS_TO")
    usuario = configuracion.get("SMTP_USERNAME")
    clave = configuracion.get("SMTP_PASSWORD")

    if not servidor or not remitente or not destinatario:
        raise EmailAvisoNoConfiguradoError(proveedor="smtp")
    if bool(usuario) != bool(clave):
        raise EmailAvisoNoConfiguradoError(proveedor="smtp")
    if not configuracion.get("SMTP_USE_TLS", True):
        raise EmailAvisoNoConfiguradoError(proveedor="smtp")

    mensaje = EmailMessage()
    mensaje["Subject"] = _asunto_reporte(reporte)
    mensaje["From"] = remitente
    mensaje["To"] = destinatario
    mensaje.set_content(_contenido_reporte(reporte))

    try:
        with smtplib.SMTP(
            servidor,
            configuracion.get("SMTP_PORT", 587),
            timeout=_TIMEOUT_SEGUNDOS,
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if usuario:
                smtp.login(usuario, clave)
            smtp.send_message(mensaje)
    except (smtplib.SMTPException, OSError):
        raise EmailAvisoEnvioError("smtp") from None


def _asunto_reporte(reporte):
    return f"Nuevo reporte ciudadano #{reporte.id} - AguaComunidad"


def _contenido_reporte(reporte):
    return "\n".join(
        [
            f"ID del reporte: {reporte.id}",
            f"Municipio: {reporte.municipio}",
            f"Barrio o vereda: {reporte.barrio}",
            f"Tipo de falla: {reporte.tipo_falla}",
            f"Fecha de recepción (UTC): {reporte.creado_en.isoformat()}",
            "",
            "Descripción:",
            reporte.descripcion,
        ]
    )
