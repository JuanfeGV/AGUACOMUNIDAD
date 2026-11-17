import io
import json
import unittest
from email.message import Message
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app import create_app
from app.extensions import db
from app.models import Municipio, ReporteCiudadano, Usuario
from app.services.semilla import MUNICIPIOS
from app.services.notificaciones import (
    EmailAvisoEnvioError,
    EmailAvisoNoConfiguradoError,
    enviar_aviso_reporte,
)
from app.utils.texto import crear_slug
from config import DevelopmentConfig


class ReportesCiudadanosTests(unittest.TestCase):
    def setUp(self):
        self.database_config_patch = patch.object(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
        self.database_config_patch.start()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all(
            Municipio(nombre=nombre, slug=crear_slug(nombre), latitud=latitud, longitud=longitud)
            for nombre, latitud, longitud in MUNICIPIOS
        )
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.database_config_patch.stop()

    def test_registra_reporte_y_muestra_confirmacion(self):
        with patch("app.blueprints.reportes.routes.enviar_aviso_reporte") as enviar_aviso:
            respuesta = self.client.post(
                "/reporte-ciudadano",
                data={
                    "municipio_id": "1",
                    "barrio": "Centro",
                    "tipo_falla": "fuga",
                    "descripcion": "Hay una fuga visible frente al parque central.",
                    "sitio_web": "",
                },
                follow_redirects=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Tu reporte qued", respuesta.data)
        self.assertEqual(ReporteCiudadano.query.count(), 1)
        enviar_aviso.assert_called_once()

    def test_error_al_guardar_no_muestra_confirmacion_ni_envia_correo(self):
        error_conexion = OperationalError("INSERT", {}, OSError("database unavailable"))
        with patch.object(db.session, "commit", side_effect=error_conexion):
            with patch.object(db.session, "rollback", wraps=db.session.rollback) as rollback:
                with patch("app.blueprints.reportes.routes.enviar_aviso_reporte") as enviar_aviso:
                    respuesta = self.client.post(
                        "/reporte-ciudadano",
                        data={
                            "municipio_id": "1",
                            "barrio": "Centro",
                            "tipo_falla": "fuga",
                            "descripcion": "Hay una fuga visible frente al parque central.",
                            "sitio_web": "",
                        },
                    )

        self.assertEqual(respuesta.status_code, 503)
        self.assertIn(b"No fue posible acceder a los datos", respuesta.data)
        self.assertNotIn(b"Tu reporte qued", respuesta.data)
        rollback.assert_called_once()
        enviar_aviso.assert_not_called()

    def test_error_al_consultar_municipios_muestra_servicio_no_disponible(self):
        error_conexion = OperationalError("SELECT", {}, OSError("database unavailable"))
        with patch.object(db.session, "scalars", side_effect=error_conexion):
            with patch.object(db.session, "rollback", wraps=db.session.rollback) as rollback:
                respuesta = self.client.get("/reporte-ciudadano")

        self.assertEqual(respuesta.status_code, 503)
        self.assertIn(b"No fue posible acceder a los datos", respuesta.data)
        self.assertNotIn(b"Tu reporte qued", respuesta.data)
        rollback.assert_called_once()

    def test_no_guarda_reporte_con_datos_obligatorios_invalidos(self):
        respuesta = self.client.post(
            "/reporte-ciudadano",
            data={
                "municipio_id": "1",
                "barrio": "",
                "tipo_falla": "fuga",
                "descripcion": "corta",
                "sitio_web": "",
            },
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(ReporteCiudadano.query.count(), 0)
        self.assertIn("Escribe el barrio o vereda.".encode(), respuesta.data)

    def test_rechaza_el_honeypot_sin_guardar_reporte(self):
        respuesta = self.client.post(
            "/reporte-ciudadano",
            data={
                "municipio_id": "1",
                "barrio": "Centro",
                "tipo_falla": "fuga",
                "descripcion": "Hay una fuga visible frente al parque central.",
                "sitio_web": "https://spam.example",
            },
        )

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(ReporteCiudadano.query.count(), 0)

    def test_correo_sin_configuracion_no_impide_guardar_y_muestra_aviso(self):
        with patch(
            "app.blueprints.reportes.routes.enviar_aviso_reporte",
            side_effect=EmailAvisoNoConfiguradoError("falta configuración"),
        ):
            respuesta = self.client.post(
                "/reporte-ciudadano",
                data={
                    "municipio_id": "1",
                    "barrio": "Centro",
                    "tipo_falla": "fuga",
                    "descripcion": "Hay una fuga visible frente al parque central.",
                    "sitio_web": "",
                },
                follow_redirects=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(ReporteCiudadano.query.count(), 1)
        self.assertIn(b"el aviso por correo est", respuesta.data)

    def test_error_de_notificacion_no_duplica_el_reporte_y_muestra_aviso(self):
        self.app.config["EMAIL_PROVIDER"] = "resend"
        with patch(
            "app.blueprints.reportes.routes.enviar_aviso_reporte",
            side_effect=EmailAvisoEnvioError("resend", 401),
        ) as enviar_aviso:
            respuesta = self.client.post(
                "/reporte-ciudadano",
                data={
                    "municipio_id": "1",
                    "barrio": "Centro",
                    "tipo_falla": "fuga",
                    "descripcion": "Hay una fuga visible frente al parque central.",
                    "sitio_web": "",
                },
                follow_redirects=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(ReporteCiudadano.query.count(), 1)
        self.assertIn(b"no se pudo enviar el aviso por correo", respuesta.data)
        enviar_aviso.assert_called_once()

    def test_bandeja_de_reportes_exige_sesion_de_administrador(self):
        respuesta = self.client.get("/admin/reportes")

        self.assertEqual(respuesta.status_code, 302)
        self.assertIn("/ingresar", respuesta.headers["Location"])

    def test_administrador_puede_ver_bandeja_de_reportes(self):
        administrador = Usuario(nombre="Admin", email="admin@example.com", rol="admin")
        administrador.set_password("una-clave-segura")
        db.session.add(administrador)
        db.session.add(
            ReporteCiudadano(
                municipio="Zipaquirá",
                barrio="Centro",
                tipo_falla="fuga",
                descripcion="Hay una fuga visible frente al parque central.",
            )
        )
        db.session.commit()

        with self.client.session_transaction() as sesion:
            sesion["_user_id"] = str(administrador.id)
            sesion["_fresh"] = True

        respuesta = self.client.get("/admin/reportes")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Reportes ciudadanos", respuesta.data)
        self.assertIn(b"Zipaquir", respuesta.data)

    def test_portada_muestra_selector_de_municipio(self):
        respuesta = self.client.get("/")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b'name="municipio"', respuesta.data)
        self.assertIn("Zipaquirá".encode(), respuesta.data)

    def test_selectores_publicos_muestran_los_once_municipios(self):
        for ruta in ("/", "/municipios", "/noticias", "/reporte-ciudadano"):
            with self.subTest(ruta=ruta):
                respuesta = self.client.get(ruta)
                self.assertEqual(respuesta.status_code, 200)
                for nombre, _, _ in MUNICIPIOS:
                    self.assertIn(nombre.encode(), respuesta.data)

    def test_selector_de_municipio_filtra_el_listado(self):
        respuesta = self.client.get("/municipios?municipio=chia")

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn("Chía".encode(), respuesta.data)
        self.assertNotIn(b'href="/municipios/zipaquira"', respuesta.data)

    def test_formulario_admin_de_articulo_muestra_los_once_municipios(self):
        administrador = Usuario(nombre="Admin", email="admin@example.com", rol="admin")
        administrador.set_password("una-clave-segura")
        db.session.add(administrador)
        db.session.commit()

        with self.client.session_transaction() as sesion:
            sesion["_user_id"] = str(administrador.id)
            sesion["_fresh"] = True

        respuesta = self.client.get("/admin/articulos/nuevo")

        self.assertEqual(respuesta.status_code, 200)
        for nombre, _, _ in MUNICIPIOS:
            self.assertIn(nombre.encode(), respuesta.data)

    def test_aviso_email_se_envia_con_starttls(self):
        self.app.config.update(
            EMAIL_PROVIDER="smtp",
            SMTP_SERVER="smtp.example.com",
            SMTP_PORT=587,
            SMTP_USE_TLS=True,
            SMTP_USERNAME="portal",
            SMTP_PASSWORD="contraseña",
            SMTP_SENDER="portal@example.com",
            EMAIL_REPORTS_TO="aguacomunidadsabana@gmail.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Hay una fuga visible frente al parque central.",
        )
        db.session.add(reporte)
        db.session.commit()

        with patch("app.services.notificaciones.smtplib.SMTP") as smtp_factory:
            enviar_aviso_reporte(reporte)

        smtp = smtp_factory.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        mensaje = smtp.send_message.call_args.args[0]
        self.assertEqual(mensaje["Subject"], f"Nuevo reporte ciudadano #{reporte.id} - AguaComunidad")
        self.assertEqual(mensaje["To"], "aguacomunidadsabana@gmail.com")
        self.assertIn("Zipaquirá", mensaje.get_content())

    def test_resend_envia_reporte_por_https_con_campos_requeridos(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test_api_key",
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Hay una fuga visible frente al parque central.",
        )
        db.session.add(reporte)
        db.session.commit()

        with patch("app.services.notificaciones.urlopen", return_value=io.BytesIO(b'{"id":"email_123"}')) as abrir_url:
            proveedor = enviar_aviso_reporte(reporte)

        self.assertEqual(proveedor, "resend")
        solicitud = abrir_url.call_args.args[0]
        self.assertEqual(solicitud.full_url, "https://api.resend.com/emails")
        self.assertEqual(solicitud.get_method(), "POST")
        cabeceras = {nombre.lower(): valor for nombre, valor in solicitud.header_items()}
        self.assertEqual(cabeceras["authorization"], "Bearer re_test_api_key")
        self.assertEqual(cabeceras["content-type"], "application/json")
        self.assertEqual(cabeceras["accept"], "application/json")
        self.assertEqual(cabeceras["user-agent"], "SabanaCentroAguaComunidad/1.0")
        self.assertEqual(abrir_url.call_args.kwargs["timeout"], 10)
        cuerpo = json.loads(solicitud.data)
        self.assertEqual(cuerpo["from"], "onboarding@resend.dev")
        self.assertEqual(cuerpo["to"], ["reportes@example.com"])
        self.assertEqual(
            cuerpo["subject"],
            f"Nuevo reporte ciudadano #{reporte.id} - AguaComunidad",
        )
        for campo in (
            f"ID del reporte: {reporte.id}",
            "Municipio: Zipaquirá",
            "Barrio o vereda: Centro",
            "Tipo de falla: fuga",
            "Descripción:",
            reporte.descripcion,
            reporte.creado_en.isoformat(),
        ):
            self.assertIn(campo, cuerpo["text"])

    def test_resend_error_de_autenticacion_no_expone_respuesta(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test_api_key",
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Descripción privada",
        )
        db.session.add(reporte)
        db.session.commit()
        error_http = HTTPError(
            "https://api.resend.com/emails",
            401,
            "Unauthorized",
            None,
            io.BytesIO(b"private provider response"),
        )

        with patch("app.services.notificaciones.urlopen", side_effect=error_http):
            with self.assertRaises(EmailAvisoEnvioError) as error:
                enviar_aviso_reporte(reporte)

        self.assertEqual(error.exception.proveedor, "resend")
        self.assertEqual(error.exception.estado_http, 401)
        self.assertNotIn("re_test_api_key", str(error.exception))
        self.assertNotIn("private provider response", str(error.exception))

    def test_resend_http_errors_registran_diagnostico_seguro(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test_api_key",
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Descripción privada",
        )
        db.session.add(reporte)
        db.session.commit()
        respuestas = (
            (401, "invalid_api_key"),
            (403, "validation_error"),
            (422, "unprocessable_entity"),
        )

        for codigo, nombre in respuestas:
            with self.subTest(codigo=codigo):
                cuerpo = json.dumps(
                    {
                        "statusCode": codigo,
                        "name": nombre,
                        "message": (
                            "Diagnostic cause; private@example.com; "
                            "Bearer re_test_api_key; API key=re_test_api_key; "
                            "unlabelledtoken123456789012345678901234567890; "
                            "Zipaquirá; Centro; fuga; Descripción privada"
                        ),
                    }
                ).encode("utf-8")
                error_http = HTTPError(
                    "https://api.resend.com/emails",
                    codigo,
                    "Request rejected",
                    None,
                    io.BytesIO(cuerpo),
                )

                with self.assertLogs("app.services.notificaciones", level="WARNING") as registros:
                    with patch("app.services.notificaciones.urlopen", side_effect=error_http):
                        with self.assertRaises(EmailAvisoEnvioError):
                            enviar_aviso_reporte(reporte)

                diagnostico = "\n".join(registros.output)
                self.assertIn(f"http_status={codigo}", diagnostico)
                self.assertIn(f"statusCode={codigo}", diagnostico)
                self.assertIn(f"name={nombre}", diagnostico)
                self.assertIn("Diagnostic cause", diagnostico)
                for dato_privado in (
                    "re_test_api_key",
                    "unlabelledtoken123456789012345678901234567890",
                    "private@example.com",
                    "onboarding@resend.dev",
                    "reportes@example.com",
                    "Zipaquirá",
                    "Centro",
                    "fuga",
                    "Descripción privada",
                ):
                    self.assertNotIn(dato_privado, diagnostico)

    def test_resend_respuesta_no_json_registra_diagnostico_generico_seguro(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test_api_key",
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Descripción privada",
        )
        db.session.add(reporte)
        db.session.commit()
        cabeceras = Message()
        cabeceras["Content-Type"] = "text/html; charset=utf-8"
        cabeceras["X-Request-Id"] = "req_abc123"
        cabeceras["CF-Ray"] = "9edb1234567890ab-SJC"
        cabeceras["X-Correlation-Id"] = "re_test_api_key"
        error_http = HTTPError(
            "https://api.resend.com/emails",
            403,
            "Forbidden",
            cabeceras,
            io.BytesIO(
                "private@example.com re_test_api_key Descripción privada".encode("utf-8")
            ),
        )

        with self.assertLogs("app.services.notificaciones", level="WARNING") as registros:
            with patch("app.services.notificaciones.urlopen", side_effect=error_http):
                with self.assertRaises(EmailAvisoEnvioError):
                    enviar_aviso_reporte(reporte)

        diagnostico = "\n".join(registros.output)
        self.assertIn("http_status=403", diagnostico)
        self.assertIn("content_type=text/html; charset=utf-8", diagnostico)
        self.assertIn(
            "request_ids=x-request-id=req_abc123,cf-ray=9edb1234567890ab-SJC",
            diagnostico,
        )
        self.assertIn("message=non_json_response", diagnostico)
        self.assertNotIn("private@example.com", diagnostico)
        self.assertNotIn("re_test_api_key", diagnostico)
        self.assertNotIn("Descripción privada", diagnostico)

    def test_resend_falla_de_red_y_timeout_sin_exponer_detalles(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY="re_test_api_key",
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Descripción privada",
        )
        db.session.add(reporte)
        db.session.commit()

        for error_envio in (URLError("detalle privado"), TimeoutError("detalle privado")):
            with self.subTest(error=type(error_envio).__name__):
                with patch("app.services.notificaciones.urlopen", side_effect=error_envio):
                    with self.assertRaises(EmailAvisoEnvioError) as error:
                        enviar_aviso_reporte(reporte)
                self.assertEqual(error.exception.proveedor, "resend")
                self.assertNotIn("detalle privado", str(error.exception))

    def test_resend_aceptado_muestra_confirmacion_sin_asegurar_entrega(self):
        self.app.config["EMAIL_PROVIDER"] = "resend"
        with patch(
            "app.blueprints.reportes.routes.enviar_aviso_reporte",
            return_value="resend",
        ) as enviar_aviso:
            respuesta = self.client.post(
                "/reporte-ciudadano",
                data={
                    "municipio_id": "1",
                    "barrio": "Centro",
                    "tipo_falla": "fuga",
                    "descripcion": "Hay una fuga visible frente al parque central.",
                    "sitio_web": "",
                },
                follow_redirects=True,
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertIn(b"Resend acept", respuesta.data)
        self.assertIn(b"no confirma la entrega", respuesta.data)
        self.assertEqual(ReporteCiudadano.query.count(), 1)
        enviar_aviso.assert_called_once()

    def test_resend_sin_configuracion_no_intenta_enviar(self):
        self.app.config.update(
            EMAIL_PROVIDER="resend",
            RESEND_API_KEY=None,
            RESEND_FROM="onboarding@resend.dev",
            EMAIL_REPORTS_TO="reportes@example.com",
        )
        reporte = ReporteCiudadano(
            municipio="Zipaquirá",
            barrio="Centro",
            tipo_falla="fuga",
            descripcion="Descripción privada",
        )

        with patch("app.services.notificaciones.urlopen") as abrir_url:
            with self.assertRaises(EmailAvisoNoConfiguradoError):
                enviar_aviso_reporte(reporte)

        abrir_url.assert_not_called()

    def test_destinatario_predeterminado_de_reportes(self):
        self.assertEqual(self.app.config["EMAIL_REPORTS_TO"], "aguacomunidadsabana@gmail.com")


if __name__ == "__main__":
    unittest.main()
