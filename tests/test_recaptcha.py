import io
import json
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs
from unittest.mock import patch

from flask import Flask

from app.services.recaptcha import (
    RecaptchaConfiguracionError,
    RecaptchaNoDisponibleError,
    verificar_token_recaptcha,
)


class RecaptchaServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            RECAPTCHA_ENABLED=True,
            RECAPTCHA_ALLOW_DISABLED=False,
            RECAPTCHA_SITE_KEY="site_key_test",
            RECAPTCHA_SECRET_KEY="secret_key_test",
            RECAPTCHA_ALLOWED_HOSTNAMES=("aguacomunidad.up.railway.app/",),
        )

    def test_token_valido_se_verifica_por_https_y_exige_hostname_permitido(self):
        respuesta_google = io.BytesIO(
            json.dumps(
                {
                    "success": True,
                    "hostname": "aguacomunidad.up.railway.app",
                }
            ).encode("utf-8")
        )

        with self.app.app_context():
            with patch(
                "app.services.recaptcha.urlopen",
                return_value=respuesta_google,
            ) as abrir_url:
                self.assertTrue(verificar_token_recaptcha("token-valido-de-prueba"))

        solicitud = abrir_url.call_args.args[0]
        self.assertEqual(
            solicitud.full_url,
            "https://www.google.com/recaptcha/api/siteverify",
        )
        self.assertEqual(solicitud.get_method(), "POST")
        self.assertEqual(
            parse_qs(solicitud.data.decode("ascii")),
            {
                "secret": ["secret_key_test"],
                "response": ["token-valido-de-prueba"],
            },
        )
        self.assertEqual(abrir_url.call_args.kwargs["timeout"], 10)

    def test_token_faltante_no_consulta_google(self):
        with self.app.app_context():
            with patch("app.services.recaptcha.urlopen") as abrir_url:
                self.assertFalse(verificar_token_recaptcha(""))

        abrir_url.assert_not_called()

    def test_google_rechaza_token_invalido_o_vencido(self):
        respuestas = (
            {"success": False, "error-codes": ["invalid-input-response"]},
            {"success": False, "error-codes": ["timeout-or-duplicate"]},
        )

        for resultado in respuestas:
            with self.subTest(resultado=resultado):
                with self.app.app_context():
                    with patch(
                        "app.services.recaptcha.urlopen",
                        return_value=io.BytesIO(json.dumps(resultado).encode("utf-8")),
                    ):
                        self.assertFalse(verificar_token_recaptcha("token-de-prueba"))

    def test_hostname_no_autorizado_es_rechazado(self):
        respuesta_google = io.BytesIO(
            b'{"success":true,"hostname":"sitio-no-autorizado.example"}'
        )

        with self.app.app_context():
            with patch("app.services.recaptcha.urlopen", return_value=respuesta_google):
                self.assertFalse(verificar_token_recaptcha("token-de-prueba"))

    def test_timeout_y_error_de_red_no_divulgan_token_ni_claves(self):
        for error in (TimeoutError("secret_key_test token-de-prueba"), URLError("datos privados")):
            with self.subTest(error=type(error).__name__):
                with self.app.app_context():
                    with self.assertLogs("app.services.recaptcha", level="WARNING") as registros:
                        with patch(
                            "app.services.recaptcha.urlopen",
                            side_effect=error,
                        ):
                            with self.assertRaises(RecaptchaNoDisponibleError):
                                verificar_token_recaptcha("token-de-prueba")

                texto_logs = "\n".join(registros.output)
                self.assertNotIn("secret_key_test", texto_logs)
                self.assertNotIn("token-de-prueba", texto_logs)
                self.assertNotIn("datos privados", texto_logs)

    def test_configuracion_incompleta_o_captcha_deshabilitado_en_produccion_falla_cerrado(self):
        configuraciones = (
            {
                "RECAPTCHA_ENABLED": True,
                "RECAPTCHA_SECRET_KEY": None,
            },
            {
                "RECAPTCHA_ENABLED": False,
                "RECAPTCHA_SECRET_KEY": "secret_key_test",
            },
        )

        for cambios in configuraciones:
            with self.subTest(cambios=cambios):
                configuracion = {
                    "TESTING": False,
                    "DEBUG": False,
                    "RECAPTCHA_ALLOW_DISABLED": False,
                    "RECAPTCHA_ENABLED": True,
                    "RECAPTCHA_SECRET_KEY": "secret_key_test",
                }
                configuracion.update(cambios)
                self.app.config.update(configuracion)
                with self.app.app_context():
                    with self.assertLogs("app.services.recaptcha", level="ERROR") as registros:
                        with self.assertRaises(RecaptchaConfiguracionError):
                            verificar_token_recaptcha("token-de-prueba")

                texto_logs = "\n".join(registros.output)
                self.assertNotIn("secret_key_test", texto_logs)
                self.assertNotIn("token-de-prueba", texto_logs)

    def test_entorno_de_pruebas_puede_desactivar_captcha_sin_token(self):
        self.app.config.update(TESTING=True, RECAPTCHA_ENABLED=False)

        with self.app.app_context():
            with patch("app.services.recaptcha.urlopen") as abrir_url:
                self.assertTrue(verificar_token_recaptcha(""))

        abrir_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
