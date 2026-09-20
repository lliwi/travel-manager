"""Settings as the place decisions live, and mail configured from there.

Three parameters used to live only in the environment: the OCR languages, the
default timezone and the default locale. Changing any of them meant editing a
file and redeploying, which is the thing invariant 7 exists to avoid for the AI
provider -- and they are the same kind of decision.

The mail password is the reason «secreto» exists as a type: a credential in a
column anyone can SELECT is a credential in plain text, whichever table it
sits in.
"""
import pytest

from app.extensions import db
from app.services import mail_service, settings_service


@pytest.mark.unit
class TestLoQueSaleDelEntorno:
    def test_los_idiomas_de_ocr_se_leen_de_ajustes(self, app, seeded):
        settings_service.set_value('OCR_IDIOMAS', 'spa+fra')

        assert settings_service.idiomas_ocr() == 'spa+fra'

    def test_la_zona_horaria_se_lee_de_ajustes(self, app, seeded):
        settings_service.set_value('ZONA_HORARIA_POR_DEFECTO', 'Atlantic/Canary')

        assert settings_service.zona_horaria_por_defecto() == 'Atlantic/Canary'

    def test_el_idioma_se_lee_de_ajustes(self, app, seeded):
        settings_service.set_value('IDIOMA_POR_DEFECTO', 'ca')

        assert settings_service.idioma_por_defecto() == 'ca'

    def test_sin_fila_todavia_responde_el_arranque(self, app):
        """A fresh install reads a setting before anything has seeded one.

        The environment keeps answering until the row exists, and stops
        mattering the moment it does.
        """
        assert settings_service.zona_horaria_por_defecto()
        assert settings_service.idiomas_ocr()

    def test_una_cuenta_nueva_nace_en_la_zona_configurada(self, app, seeded, admin):
        """Where the setting actually decides.

        Every account carries a timezone -- the column is NOT NULL -- and a
        trip takes it from whoever creates it, so a column default of
        «Europe/Madrid» quietly overrode the setting for everybody. The
        organisation's answer belongs at the moment the account is made.
        """
        from app.services import user_service

        settings_service.set_value('ZONA_HORARIA_POR_DEFECTO', 'Atlantic/Canary')
        settings_service.set_value('IDIOMA_POR_DEFECTO', 'ca')

        nuevo = user_service.create_user(
            admin, 'canario', 'canario@corp.test', 'Canario',
            'Contrasena-Muy-Segura-2026', role_codes=['usuario'],
        )

        assert nuevo.zona_horaria == 'Atlantic/Canary'
        assert nuevo.idioma == 'ca'

    def test_un_documento_sin_persona_usa_la_configurada(self, app, seeded):
        """Normalisation has no actor to ask, so the setting is the answer."""
        from app.services import normalization_service

        settings_service.set_value('ZONA_HORARIA_POR_DEFECTO', 'Atlantic/Canary')

        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'nombre': 'Algo',
                       'inicio': {'local': '2026-06-01T10:00', 'zona_horaria': None}},
            'confianzas': {},
        }]}, clasificacion='otro')

        zona = resultado.servicios[0]['campos']['inicio']['zona_horaria']
        assert zona == 'Atlantic/Canary'

    def test_la_zona_de_la_persona_manda_sobre_la_general(self, app, seeded, gestor):
        from datetime import datetime

        from app.services import trip_service

        settings_service.set_value('ZONA_HORARIA_POR_DEFECTO', 'Atlantic/Canary')
        gestor.zona_horaria = 'Europe/Madrid'
        db.session.commit()

        trip = trip_service.create_trip(
            gestor, titulo='Peninsular', inicio_local=datetime(2026, 6, 1, 10, 0),
        )

        assert trip.inicio_tz == 'Europe/Madrid'


@pytest.mark.security
class TestLaContrasenaNoSeGuardaEnClaro:
    def test_se_cifra_al_escribirla(self, app, seeded):
        from app.models.settings import SystemSetting

        settings_service.set_value('CORREO_CONTRASENA', 'la-de-verdad')

        fila = SystemSetting.query.filter_by(clave='CORREO_CONTRASENA').first()
        assert 'la-de-verdad' not in str(fila.valor)

    def test_se_descifra_al_leerla(self, app, seeded):
        settings_service.set_value('CORREO_CONTRASENA', 'la-de-verdad')

        assert settings_service.get('CORREO_CONTRASENA') == 'la-de-verdad'

    def test_la_pantalla_no_la_devuelve(self, as_user, admin, seeded):
        """Not even to the administrator who typed it."""
        settings_service.set_value('CORREO_CONTRASENA', 'la-de-verdad')

        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'la-de-verdad' not in html
        assert 'guardada — escriba para cambiarla' in html

    def test_guardar_sin_tocarla_la_conserva(self, as_user, admin, seeded):
        """The field arrives empty on every load; treating that as a change
        would wipe the password whenever anybody saved the page."""
        settings_service.set_value('CORREO_CONTRASENA', 'la-de-verdad')

        with as_user(admin) as client:
            client.post('/admin/ajustes', data={
                'ajuste__CORREO_CONTRASENA': '',
                'ajuste__CORREO_HOST': 'smtp.corp.test',
            }, follow_redirects=True)

        assert settings_service.get('CORREO_CONTRASENA') == 'la-de-verdad'


@pytest.mark.unit
class TestElEnvioDeCorreo:
    def test_desactivado_no_envia_ni_falla(self, app, seeded):
        """«No configurado» es un estado legítimo, no un error.

        Convertirlo en excepción obligaría a cada llamante a manejar una
        situación que no es un problema.
        """
        settings_service.set_value('CORREO_HABILITADO', False)

        assert mail_service.enviar(['a@corp.test'], 'x', 'y') is False

    def test_sin_servidor_no_envia(self, app, seeded):
        settings_service.set_value('CORREO_HABILITADO', True)
        settings_service.set_value('CORREO_HOST', '')

        assert not mail_service.esta_configurado()

    def test_sin_destinatario_no_envia(self, app, seeded):
        assert mail_service.enviar([], 'x', 'y') is False

    def test_un_fallo_real_si_se_reporta(self, app, seeded):
        from app.services.mail_service import MailError

        settings_service.set_value('CORREO_HABILITADO', True)
        settings_service.set_value('CORREO_HOST', 'servidor.inexistente.test')
        settings_service.set_value('CORREO_PUERTO', 1)
        settings_service.set_value('CORREO_REMITENTE', 'de@corp.test')

        with pytest.raises(MailError):
            mail_service.enviar(['a@corp.test'], 'x', 'y')


@pytest.mark.unit
class TestLaPantallaEstaOrganizada:
    def test_cada_grupo_declarado_tiene_nombre(self):
        """A group with no label showed as its own slug: «investigacion»."""
        grupos_usados = {v[2] for v in settings_service.DEFAULTS.values()}

        sin_etiqueta = grupos_usados - set(settings_service.ETIQUETAS_GRUPO)

        assert not sin_etiqueta, f'Sin nombre para la pantalla: {sin_etiqueta}'

    def test_los_proveedores_aparecen_en_ajustes(self, as_user, admin, seeded):
        """Where people come looking for them."""
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'Proveedores de inteligencia artificial' in html


@pytest.mark.integration
class TestLosProveedoresVivenEnAjustes:
    """Moved rather than linked: one place to configure how the system thinks.

    They keep their own screens because they are rows with a life of their own
    -- encrypted keys, connection tests, a task bound to each -- but the way in
    is Ajustes, and the address says so too.
    """

    def test_la_url_cuelga_de_ajustes(self, as_user, admin, seeded):
        with as_user(admin) as client:
            assert client.get('/admin/ajustes/proveedores').status_code == 200

    def test_la_url_anterior_ya_no_existe(self, as_user, admin, seeded):
        with as_user(admin) as client:
            assert client.get('/admin/ia/proveedores').status_code == 404

    def test_se_llega_desde_ajustes(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'Proveedores de inteligencia artificial' in html
        assert '/admin/ajustes/proveedores' in html

    def test_ya_no_esta_en_el_menu(self, as_user, admin, seeded):
        """Two doors to one thing is two places to look when it is wrong."""
        with as_user(admin) as client:
            html = client.get('/dashboard/').get_data(as_text=True)

        assert 'Proveedores de IA' not in html

    def test_las_migas_pasan_por_ajustes(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes/proveedores').get_data(as_text=True)

        assert '/admin/ajustes"' in html
