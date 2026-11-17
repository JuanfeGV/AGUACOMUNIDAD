import json
import smtplib
import ssl
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app


_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT_SEGUNDOS = 10


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
        },
        method="POST",
    )

    try:
        with urlopen(solicitud, timeout=_TIMEOUT_SEGUNDOS) as respuesta:
            estado_http = getattr(respuesta, "status", 200)
            if not 200 <= estado_http < 300:
                raise EmailAvisoEnvioError("resend", estado_http)
    except HTTPError as error:
        raise EmailAvisoEnvioError("resend", error.code) from None
    except (URLError, TimeoutError, OSError):
        raise EmailAvisoEnvioError("resend") from None


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
