"""Cómo se ven y se usan los formularios.

Lo que se fija aquí es lo que se rompe sin que falle nada en el servidor: un
botón de guardar que sale gris y sin estilo porque alguien escribió
«{{ form.submit }}» a pelo, un mensaje de error en inglés en mitad de una
interfaz en español, o un campo cuya ayuda un lector de pantalla no llega a
leer porque no está enlazada a él.
"""
import re
from pathlib import Path

import pytest

PLANTILLAS = Path(__file__).resolve().parent.parent / 'app' / 'templates'


@pytest.mark.unit
class TestLosBotonesDeEnviar:
    def test_ninguno_se_pinta_sin_clase(self):
        """Without a class it renders as the browser's grey native button."""
        sueltos = [
            str(ruta.relative_to(PLANTILLAS))
            for ruta in PLANTILLAS.rglob('*.html')
            if re.search(r'\{\{\s*form\.submit\s*(\(\s*\))?\s*\}\}', ruta.read_text())
        ]

        assert not sueltos, 'Botón de enviar sin estilo en: ' + ', '.join(sueltos)


@pytest.mark.integration
class TestLosMensajesDeWTForms:
    def test_salen_en_espanol(self, app):
        """«This field is required.» in the middle of a Spanish interface."""
        from app.blueprints.admin.forms import UserForm

        with app.test_request_context(method='POST', data={}):
            form = UserForm()
            form.validate()

        mensajes = [m for errores in form.errors.values() for m in errores]
        assert mensajes
        assert not [m for m in mensajes if 'This field' in m], mensajes

    def test_todos_los_formularios_heredan_la_base(self):
        """A form on FlaskForm directly would speak English again."""
        raiz = PLANTILLAS.parent / 'blueprints'
        directos = [
            f'{ruta.parent.name}/{ruta.name}'
            for ruta in raiz.rglob('*.py')
            if re.search(r'class \w+\(FlaskForm\)', ruta.read_text())
        ]

        assert not directos, directos


@pytest.mark.integration
class TestElMacroDeCampos:
    @pytest.fixture
    def html(self, as_user, admin):
        with as_user(admin) as client:
            return client.post('/admin/usuarios/nuevo', data={
                'username': 'x', 'email': 'no-es-un-correo',
            }).get_data(as_text=True)

    def test_marca_lo_opcional_y_no_lo_obligatorio(self, html):
        etiqueta = re.search(r'<label[^>]*for="apellidos"[^>]*>(.*?)</label>', html, re.S)
        obligatoria = re.search(r'<label[^>]*for="email"[^>]*>(.*?)</label>', html, re.S)

        assert 'opcional' in etiqueta.group(1)
        assert 'opcional' not in obligatoria.group(1)

    def test_un_desplegable_no_se_marca_opcional(self, html):
        """It always carries a value; «opcional» would be untrue."""
        etiqueta = re.search(r'<label[^>]*for="estado"[^>]*>(.*?)</label>', html, re.S)

        assert 'opcional' not in etiqueta.group(1)

    def test_el_error_queda_enlazado_al_campo(self, html):
        campo = re.search(r'<input[^>]*id="email"[^>]*>', html).group(0)

        assert 'aria-invalid="true"' in campo
        assert 'aria-describedby="email-error"' in campo
        assert 'id="email-error"' in html

    def test_los_roles_son_un_grupo_con_nombre(self, html):
        """A screen reader announces «Roles» on entering, not just «casilla»."""
        assert re.search(r'<fieldset[^>]*>\s*<legend[^>]*>Roles', html)


@pytest.mark.integration
class TestLosAjustesSeDimensionanSegunLoQueLlevan:
    @pytest.fixture
    def html(self, as_user, admin):
        with as_user(admin) as client:
            return client.get('/admin/ajustes').get_data(as_text=True)

    def _fila(self, html, clave):
        indice = html.index(f'id="ajuste__{clave}"')
        return html[html.rindex('<div class="setting-row', 0, indice):indice]

    def test_un_dn_ocupa_la_fila_entera(self, html):
        """45 characters in a 240 px box: it was cut in half."""
        for clave in ('LDAP_USUARIO', 'LDAP_RUTA', 'PROXY_SALIDA', 'PROXY_EXCEPCIONES'):
            assert 'es-largo' in self._fila(html, clave), clave

    def test_un_numero_o_un_codigo_no_llena_la_columna(self, html):
        for clave in ('CORREO_PUERTO', 'POLITICA_MONEDA', 'DOCUMENTOS_UMBRAL_REVISION'):
            assert 'es-corto' in self._fila(html, clave), clave

    def test_un_host_va_en_la_columna(self, html):
        assert 'es-medio' in self._fila(html, 'LDAP_SERVIDOR')


@pytest.mark.security
class TestLaExigenciaDeSegundoFactorNoSeTeclea:
    """As a text box, a typo read as «ninguno» and switched MFA off silently."""

    def test_es_un_desplegable_con_los_cuatro_niveles(self, as_user, admin):
        from app.services.mfa_service import NIVELES

        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        bloque = re.search(r'<select[^>]*id="ajuste__MFA_OBLIGATORIO".*?</select>', html, re.S)
        assert bloque
        assert set(re.findall(r'value="([^"]+)"', bloque.group(0))) == set(NIVELES)

    def test_un_valor_que_no_existe_se_rechaza(self, app, seeded):
        from app.services import settings_service
        from app.utils.errors import ValidationError

        with pytest.raises(ValidationError, match='no es un valor válido'):
            settings_service.set_value('MFA_OBLIGATORIO', 'Todos')

    def test_y_no_deja_guardado_nada_a_medias(self, as_user, admin):
        """One form: a refusal must not leave the settings before it written."""
        from app.services import settings_service

        with as_user(admin) as client:
            respuesta = client.post('/admin/ajustes', data={
                'ajuste__CORREO_PUERTO': '2525',
                'ajuste__MFA_OBLIGATORIO': 'administrador',
            }, follow_redirects=True)

        assert 'no es un valor válido' in respuesta.get_data(as_text=True)
        assert settings_service.get('CORREO_PUERTO') != 2525


@pytest.mark.integration
class TestLasExplicacionesDeLosAjustesSeActualizan:
    """Code-owned text reached new installations only, until now."""

    def test_la_descripcion_se_refresca_y_el_valor_no(self, app, seeded):
        from app.extensions import db
        from app.models.settings import SystemSetting
        from app.services import settings_service

        settings_service.set_value('CORREO_PUERTO', 2525)
        fila = SystemSetting.query.filter_by(clave='CORREO_PUERTO').one()
        fila.descripcion = 'Texto de una versión anterior.'
        db.session.commit()

        settings_service.seed_defaults()

        fila = SystemSetting.query.filter_by(clave='CORREO_PUERTO').one()
        assert fila.descripcion == settings_service.DEFAULTS['CORREO_PUERTO'][4]
        assert settings_service.get('CORREO_PUERTO') == 2525
