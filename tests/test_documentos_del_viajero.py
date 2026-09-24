"""Pasaportes, visados y seguros de quien viaja.

La alerta «documento del viajero próximo a caducar» existía desde el principio
y no podía saltar nunca: el motor leía esta tabla y **nadie la escribía**. No
había servicio, ni ruta, ni pantalla. La regla, el interruptor y la columna
estaban; el camino por el que entra el dato, no.

Lo delicado aquí es de quién es el dato. Un pasaporte es de la persona, no del
viaje, así que lo gestionan ella y un administrador. Un gestor de viajes no:
para planificar necesita saber que un documento caduca antes del viaje, que es
lo que dice la alerta, y eso es distinto de leer el número de pasaporte de
alguien.
"""
from datetime import date, timedelta

import pytest

from app.services import settings_service
from app.services import traveler_document_service as servicio
from app.utils.errors import AuthorizationError, ValidationError

pytestmark = pytest.mark.integration


@pytest.fixture
def habilitado(app, seeded):
    settings_service.set_value('DOCUMENTOS_VIAJERO_HABILITADOS', True)


def _pasaporte(actor, propietario, caduca=None, numero='ABC123456'):
    return servicio.crear(
        actor, propietario, tipo='pasaporte', numero=numero, pais_emisor='ES',
        fecha_caducidad=caduca or (date.today() + timedelta(days=400)),
    )


class TestDeQuienEsEsteDato:
    def test_uno_gestiona_los_suyos(self, habilitado, viajero):
        assert servicio.puede_gestionar(viajero, viajero) is True

    def test_un_administrador_tambien(self, habilitado, admin, viajero):
        assert servicio.puede_gestionar(admin, viajero) is True

    def test_un_gestor_de_viajes_no(self, habilitado, gestor, viajero):
        """La alerta le dice que caduca antes del viaje, que es lo que
        planificar necesita. El número de pasaporte no lo es."""
        assert servicio.puede_gestionar(gestor, viajero) is False

    def test_ni_otro_viajero(self, habilitado, viajero, otro_viajero):
        assert servicio.puede_gestionar(otro_viajero, viajero) is False

    def test_y_el_servicio_lo_hace_cumplir(self, habilitado, gestor, viajero):
        with pytest.raises(AuthorizationError):
            servicio.listar(gestor, viajero)


class TestQueSeGuardaYComo:
    def test_el_numero_no_queda_en_claro(self, habilitado, viajero):
        """Solo los cuatro últimos, para poder reconocerlo sin exponerlo."""
        doc = _pasaporte(viajero, viajero, numero='ABC123456')

        assert doc.numero_ultimos4 == '3456'
        assert 'ABC123456' not in (doc.numero_encrypted or '')

    def test_pero_su_dueño_puede_leerlo_entero(self, habilitado, viajero):
        doc = _pasaporte(viajero, viajero, numero='ABC123456')

        assert servicio.numero_en_claro(viajero, viajero, doc) == 'ABC123456'

    def test_y_leerlo_queda_registrado(self, habilitado, viajero):
        """Ver la lista no deja rastro; pedir el número entero sí, porque son
        dos actos distintos."""
        from app.models.audit import AuditEvent

        doc = _pasaporte(viajero, viajero)
        antes = AuditEvent.query.filter_by(
            accion='traveler_document.number_read').count()

        servicio.numero_en_claro(viajero, viajero, doc)

        assert AuditEvent.query.filter_by(
            accion='traveler_document.number_read').count() == antes + 1

    def test_la_auditoria_nunca_lleva_el_numero(self, habilitado, viajero):
        import json

        from app.models.audit import AuditEvent

        _pasaporte(viajero, viajero, numero='SECRETO99')
        eventos = AuditEvent.query.filter(
            AuditEvent.accion.like('traveler_document%')).all()

        for evento in eventos:
            assert 'SECRETO99' not in json.dumps(evento.metadatos or {})

    def test_editar_sin_numero_conserva_el_que_habia(self, habilitado, viajero):
        """El formulario nunca lo devuelve, así que en blanco significa «déjalo
        como está», no «bórralo»."""
        doc = _pasaporte(viajero, viajero, numero='ABC123456')

        servicio.actualizar(viajero, viajero, doc, pais_emisor='FR')

        assert servicio.numero_en_claro(viajero, viajero, doc) == 'ABC123456'
        assert doc.pais_emisor == 'FR'

    def test_eliminar_borra_de_verdad(self, habilitado, viajero):
        """Es un dato personal que alguien pidió quitar; guardar una copia por
        orden es lo contrario de lo que pidió."""
        from app.models.settings import TravelerDocument

        doc = _pasaporte(viajero, viajero)
        servicio.eliminar(viajero, viajero, doc)

        assert TravelerDocument.query.count() == 0


class TestLoQueNoSeAcepta:
    def test_una_caducidad_anterior_a_la_emision(self, habilitado, viajero):
        with pytest.raises(ValidationError):
            servicio.crear(
                viajero, viajero, tipo='pasaporte',
                fecha_emision=date(2026, 5, 1),
                fecha_caducidad=date(2020, 5, 1),
            )

    def test_un_año_tecleado_de_mas(self, habilitado, viajero):
        """Convierte un pasaporte caducado en uno válido medio siglo, y la
        alerta deja de saltar sin que nada falle."""
        with pytest.raises(ValidationError):
            servicio.crear(viajero, viajero, tipo='pasaporte',
                           fecha_caducidad=date(2226, 1, 1))

    def test_con_la_funcionalidad_apagada_no_se_guarda(self, app, seeded, viajero):
        settings_service.set_value('DOCUMENTOS_VIAJERO_HABILITADOS', False)

        with pytest.raises(ValidationError) as exc:
            _pasaporte(viajero, viajero)

        assert 'Ajustes' in exc.value.mensaje


class TestLaAlertaYaPuedeSaltar:
    def test_el_motor_encuentra_lo_que_se_registra(self, habilitado, gestor,
                                                   viajero, trip, traveler_row):
        """Es la razón de todo esto: la regla leía una tabla que nadie
        escribía, así que no podía avisar de nada."""
        from app.services.alerts import engine

        _pasaporte(viajero, viajero, caduca=date.today() + timedelta(days=20))
        contexto = engine.build_context(trip)

        assert contexto.traveler_documents.get(viajero.id)


class TestLaPantalla:
    def test_se_llega_desde_el_perfil(self, as_user, viajero, habilitado):
        """Una pantalla a la que no se llega es una pantalla que no existe."""
        with as_user(viajero) as client:
            html = client.get('/auth/perfil').get_data(as_text=True)

        assert '/auth/perfil/documentos' in html

    def test_se_pueden_registrar_desde_ella(self, as_user, viajero, habilitado):
        from app.models.settings import TravelerDocument

        with as_user(viajero) as client:
            client.post('/auth/perfil/documentos/nuevo', data={
                'csrf_token': 'x', 'tipo': 'pasaporte', 'numero': 'XY9988776',
                'pais_emisor': 'ES', 'fecha_caducidad': '2030-01-15',
            }, follow_redirects=True)

        doc = TravelerDocument.query.filter_by(user_id=viajero.id).first()

        assert doc is not None
        assert doc.numero_ultimos4 == '8776'

    def test_la_lista_no_enseña_el_numero_entero(self, as_user, viajero, habilitado):
        _pasaporte(viajero, viajero, numero='XY9988776')

        with as_user(viajero) as client:
            html = client.get('/auth/perfil/documentos').get_data(as_text=True)

        assert 'XY9988776' not in html
        assert '8776' in html

    def test_un_gestor_no_entra_en_los_de_otro(self, as_user, gestor, viajero,
                                               habilitado):
        with as_user(gestor) as client:
            respuesta = client.get(f'/auth/usuarios/{viajero.id}/documentos')

        assert respuesta.status_code == 403

    def test_un_administrador_si(self, as_user, admin, viajero, habilitado):
        with as_user(admin) as client:
            respuesta = client.get(f'/auth/usuarios/{viajero.id}/documentos')

        assert respuesta.status_code == 200

    def test_apagado_la_pantalla_dice_donde_encenderlo(self, as_user, viajero,
                                                       app, seeded):
        """Callar dejaría a alguien buscando un botón que no existe."""
        settings_service.set_value('DOCUMENTOS_VIAJERO_HABILITADOS', False)

        with as_user(viajero) as client:
            html = client.get('/auth/perfil/documentos').get_data(as_text=True)

        assert 'Ajustes' in html


class TestLosEnlacesDeLaPantallaLlevanAAlgunSitio:
    """El botón «Añadir documento» daba 404.

    En Jinja, «x if cond» sin «else» deja el valor indefinido cuando la
    condición es falsa, y «url_for» lo convierte en un segmento vacío:
    «/auth/usuarios//documentos/nuevo», que no casa con ninguna regla. La
    pantalla se dibujaba perfectamente y todos sus botones estaban rotos.

    Esto recorre los enlaces que la página trae de verdad, en vez de pedir las
    URL que uno cree que genera -- que es lo que hacían las otras pruebas y
    por eso no lo vieron.
    """

    def _enlaces(self, html):
        import re

        return re.findall(r'(?:href|action)="([^"]*documentos[^"]*)"', html)

    def _pagina(self, as_user, quien, url='/auth/perfil/documentos'):
        with as_user(quien) as client:
            return client.get(url).get_data(as_text=True)

    def test_ninguno_lleva_un_segmento_vacio(self, as_user, viajero, habilitado):
        _pasaporte(viajero, viajero)

        enlaces = self._enlaces(self._pagina(as_user, viajero))

        assert enlaces
        rotos = [e for e in enlaces if '//' in e.lstrip('htps:/')]
        assert not rotos, f'URLs con un hueco: {rotos}'

    def test_y_todos_responden(self, as_user, viajero, habilitado):
        """Un 404 en un botón propio es un botón que no existe."""
        _pasaporte(viajero, viajero)
        enlaces = self._enlaces(self._pagina(as_user, viajero))

        with as_user(viajero) as client:
            fallidos = {
                url: client.get(url).status_code
                for url in enlaces
                # Los de borrar solo aceptan POST; se cubren aparte.
                if 'eliminar' not in url
            }

        assert all(codigo == 200 for codigo in fallidos.values()), fallidos

    def test_tambien_los_de_otra_persona(self, as_user, admin, viajero, habilitado):
        _pasaporte(viajero, viajero)

        html = self._pagina(as_user, admin, f'/auth/usuarios/{viajero.id}/documentos')
        enlaces = self._enlaces(html)

        with as_user(admin) as client:
            fallidos = {
                url: client.get(url).status_code
                for url in enlaces if 'eliminar' not in url
            }

        assert all(codigo == 200 for codigo in fallidos.values()), fallidos

    def test_un_identificador_inventado_es_404_y_no_un_500(self, as_user, admin,
                                                           habilitado):
        with as_user(admin) as client:
            respuesta = client.get('/auth/usuarios/no-es-un-uuid/documentos')

        assert respuesta.status_code == 404


class TestUnaCuentaDelDirectorioTambien:
    """Una cuenta LDAP es de solo lectura, pero eso no alcanza aquí.

    Lo que es de solo lectura es lo que el directorio refresca en cada inicio
    de sesión -- nombre, correo, departamento -- porque editarlo aquí parecería
    funcionar y revertiría al siguiente acceso. Un pasaporte no está en el
    directorio: lo posee esta aplicación, como los roles o el segundo factor.
    Negárselo a media plantilla porque se autentica contra el AD dejaría sin
    avisos de caducidad justo a quien más viaja.

    Se entra por el camino de verdad, contra el directorio simulado, y no con
    la contraseña local de los fixtures: una cuenta del directorio no tiene, y
    una prueba que entrara de otra forma no diría nada sobre el camino que
    recorre una persona.
    """

    def _entrar(self, client, login):
        from app.services.identity import authenticate
        from tests.test_directorio import CLAVE, _configurar

        _configurar()
        usuario = authenticate('alopez', CLAVE)
        login(usuario, CLAVE)
        return usuario

    def test_no_es_una_cuenta_local(self, app, seeded, directorio, client, login):
        """Si esto deja de ser cierto, el resto de la clase no prueba nada."""
        assert self._entrar(client, login).es_local is False

    def test_puede_gestionar_los_suyos(self, habilitado, directorio, client, login):
        usuario = self._entrar(client, login)

        assert servicio.puede_gestionar(usuario, usuario) is True

    def test_y_registrarlos(self, habilitado, directorio, client, login):
        usuario = self._entrar(client, login)

        doc = _pasaporte(usuario, usuario, numero='LDAP12345')

        assert doc.numero_ultimos4 == '2345'
        assert servicio.listar(usuario, usuario) == [doc]

    def test_el_enlace_sale_en_su_perfil(self, habilitado, directorio, client, login):
        """Su perfil se dibuja sin formulario por ser de solo lectura, y el
        enlace vive fuera de ese bloque."""
        self._entrar(client, login)

        html = client.get('/auth/perfil').get_data(as_text=True)

        assert 'se gestiona en el directorio corporativo' in html
        assert '/auth/perfil/documentos' in html

    def test_y_la_pantalla_deja_añadir(self, habilitado, directorio, client, login):
        from app.models.settings import TravelerDocument

        usuario = self._entrar(client, login)

        client.post('/auth/perfil/documentos/nuevo', data={
            'csrf_token': 'x', 'tipo': 'pasaporte', 'numero': 'LDAP99887',
            'fecha_caducidad': '2031-03-01',
        }, follow_redirects=True)

        assert TravelerDocument.query.filter_by(user_id=usuario.id).count() == 1

    def test_volver_a_entrar_no_se_los_lleva(self, habilitado, directorio,
                                             client, login):
        """El directorio refresca nombre y correo en cada acceso. Si además
        arrastrara esto, el pasaporte desaparecería al día siguiente."""
        from app.models.settings import TravelerDocument
        from app.services.identity import authenticate
        from tests.test_directorio import CLAVE

        usuario = self._entrar(client, login)
        _pasaporte(usuario, usuario)

        authenticate('alopez', CLAVE)

        assert TravelerDocument.query.filter_by(user_id=usuario.id).count() == 1

    def test_la_alerta_lo_ve_igual(self, habilitado, directorio, client, login,
                                   gestor, trip):
        """Es lo que hace útil todo esto para una plantilla que entra por AD."""
        from datetime import date, timedelta

        from app.services import trip_service
        from app.services.alerts import engine

        usuario = self._entrar(client, login)
        trip_service.add_traveler(gestor, trip, usuario)
        _pasaporte(usuario, usuario, caduca=date.today() + timedelta(days=15))

        contexto = engine.build_context(trip)

        assert contexto.traveler_documents.get(usuario.id)
