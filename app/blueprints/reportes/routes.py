import logging

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from wtforms import SelectChoice

from ...extensions import db
from ...models import Municipio, ReporteCiudadano
from ...services.notificaciones import (
    EmailAvisoEnvioError,
    EmailAvisoNoConfiguradoError,
    enviar_aviso_reporte,
)
from ...services.recaptcha import (
    RecaptchaConfiguracionError,
    RecaptchaNoDisponibleError,
    verificar_token_recaptcha,
)
from . import bp
from .forms import ReporteCiudadanoForm

logger = logging.getLogger(__name__)


def _renderizar_formulario(form, status=200):
    captcha_habilitado = current_app.config.get("RECAPTCHA_ENABLED") is True
    clave_publica = current_app.config.get("RECAPTCHA_SITE_KEY")
    hosts_permitidos = current_app.config.get("RECAPTCHA_ALLOWED_HOSTNAMES")
    captcha_configurado = bool(
        captcha_habilitado
        and clave_publica
        and current_app.config.get("RECAPTCHA_SECRET_KEY")
        and hosts_permitidos
    )
    captcha_requerido = captcha_habilitado or not (
        current_app.testing or current_app.config.get("RECAPTCHA_ALLOW_DISABLED", False)
    )

    return (
        render_template(
            "reportes/formulario.html",
            form=form,
            recaptcha_habilitado=captcha_habilitado,
            recaptcha_clave_publica=clave_publica,
            recaptcha_configurado=captcha_configurado,
            recaptcha_requerido=captcha_requerido,
        ),
        status,
    )


def _error_base_datos():
    return (
        render_template(
            "errors/error.html",
            codigo=503,
            titulo="Servicio temporalmente no disponible",
            texto="No fue posible acceder a los datos. Intenta de nuevo más tarde.",
        ),
        503,
    )


@bp.route("/reporte-ciudadano", methods=["GET", "POST"])
def index():
    form = ReporteCiudadanoForm()
    try:
        municipios = db.session.scalars(select(Municipio).order_by(Municipio.nombre)).all()
    except SQLAlchemyError:
        db.session.rollback()
        logger.error("No se pudieron consultar los municipios para el formulario de reportes.")
        return _error_base_datos()

    form.municipio_id.choices = [
        SelectChoice("0", "Selecciona un municipio")
    ] + [
        SelectChoice(str(municipio.id), municipio.nombre) for municipio in municipios
    ]

    if form.validate_on_submit():
        if form.sitio_web.data:
            abort(400)

        try:
            captcha_valido = verificar_token_recaptcha(
                request.form.get("g-recaptcha-response", "")
            )
        except (RecaptchaConfiguracionError, RecaptchaNoDisponibleError):
            flash(
                "No fue posible validar la seguridad del formulario. Intenta de nuevo más tarde.",
                "warning",
            )
            return _renderizar_formulario(form, 503)

        if not captcha_valido:
            flash(
                "Completa nuevamente la verificación antes de enviar el reporte.",
                "warning",
            )
            return _renderizar_formulario(form, 400)

        try:
            municipio = db.session.get(Municipio, form.municipio_id.data)
            if municipio is None:
                form.municipio_id.errors.append("Selecciona un municipio válido.")
                return _renderizar_formulario(form, 400)

            reporte = ReporteCiudadano(
                municipio=municipio.nombre,
                barrio=form.barrio.data,
                tipo_falla=form.tipo_falla.data,
                descripcion=form.descripcion.data,
            )
            db.session.add(reporte)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logger.error("No se pudo guardar el reporte ciudadano.")
            return _error_base_datos()

        try:
            proveedor = enviar_aviso_reporte(reporte)
        except EmailAvisoNoConfiguradoError as error:
            logger.warning(
                "No se envió aviso por correo: falta configuración (proveedor=%s).",
                error.proveedor,
            )
            flash(
                "Tu reporte quedó registrado, pero el aviso por correo está pendiente de configuración.",
                "warning",
            )
        except EmailAvisoEnvioError as error:
            logger.warning(
                "No se pudo enviar el aviso por correo (proveedor=%s, estado_http=%s).",
                error.proveedor,
                error.estado_http,
            )
            flash(
                "Tu reporte quedó registrado, pero no se pudo enviar el aviso por correo.",
                "warning",
            )
        else:
            if proveedor == "resend":
                flash(
                    "Tu reporte quedó registrado y Resend aceptó la solicitud de envío. "
                    "Esto no confirma la entrega del correo.",
                    "success",
                )
            else:
                flash(
                    "Tu reporte quedó registrado y el servidor SMTP aceptó el mensaje para envío. "
                    "Esto no confirma la entrega del correo.",
                    "success",
                )

        return redirect(url_for("reportes.confirmacion"))

    if request.method == "GET":
        logger.info(
            "reCAPTCHA al cargar formulario: habilitado=%s clave_publica_presente=%s",
            current_app.config.get("RECAPTCHA_ENABLED") is True,
            bool(current_app.config.get("RECAPTCHA_SITE_KEY")),
        )
    return _renderizar_formulario(form)


@bp.route("/reporte-ciudadano/confirmacion")
def confirmacion():
    return render_template("reportes/confirmacion.html")
