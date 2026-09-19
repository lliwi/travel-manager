"""Session identity and invalidation.

These tests exercise ``load_user`` directly rather than through two requests.
That is deliberate: within one test the requests share a Flask application
context, so Flask-Login finds the user cached in ``g`` and never re-runs the
loader. A bug in ``load_user`` is therefore invisible to an end-to-end test but
breaks every real second request — which is exactly what happened once.
"""
import uuid

import pytest

from app.extensions import db, load_user
from app.models.enums import UserStatus


@pytest.mark.security
class TestIdentidadDeSesion:
    """``get_id()`` and ``load_user()`` must be exact inverses."""

    def test_load_user_acepta_lo_que_produce_get_id(self, gestor):
        """The contract that broke: get_id() emits 'uuid:epoch'."""
        session_id = gestor.get_id()

        assert ':' in session_id, 'El identificador incluye la época de sesión.'
        cargado = load_user(session_id)

        assert cargado is not None, (
            f'load_user no supo interpretar «{session_id}», que es exactamente '
            'lo que get_id() escribe en la cookie.'
        )
        assert cargado.id == gestor.id

    def test_el_formato_es_uuid_dos_puntos_epoca(self, gestor):
        identificador, _, epoca = gestor.get_id().partition(':')

        assert uuid.UUID(identificador) == gestor.id
        assert int(epoca) == gestor.session_epoch

    def test_un_identificador_sin_epoca_se_rechaza(self, gestor):
        """A bare UUID was not issued by this application."""
        assert load_user(str(gestor.id)) is None

    @pytest.mark.parametrize('basura', [
        '', None, 'no-es-un-uuid', 'no-es-un-uuid:1', '::', ':1',
        'ffffffff-ffff-ffff-ffff-ffffffffffff:1',
    ])
    def test_un_identificador_invalido_se_rechaza(self, app, basura):
        assert load_user(basura) is None


@pytest.mark.security
class TestInvalidacionDeSesiones:
    """Bumping the epoch is what ends every open session at once."""

    def test_la_sesion_previa_deja_de_valer_al_cambiar_la_contrasena(self, gestor):
        antigua = gestor.get_id()
        assert load_user(antigua) is not None

        from app.services.identity import get_provider

        get_provider('local').change_password(
            str(gestor.id), None, 'Contrasena-Nueva-Test-2026'
        )

        assert load_user(antigua) is None, (
            'Cambiar la contraseña debe invalidar las sesiones abiertas.'
        )
        assert load_user(gestor.get_id()) is not None, (
            'La sesión nueva sí debe valer.'
        )

    def test_la_sesion_deja_de_valer_al_cambiar_de_rol(self, gestor, seeded):
        from app.models.enums import RoleCode
        from app.models.user import Role

        antigua = gestor.get_id()
        gestor.add_role(Role.get(RoleCode.ADMINISTRADOR))
        db.session.commit()

        assert load_user(antigua) is None

    def test_una_cuenta_desactivada_no_carga(self, gestor):
        identificador = gestor.get_id()
        assert load_user(identificador) is not None

        gestor.estado = UserStatus.INACTIVO
        db.session.commit()

        assert load_user(identificador) is None

    def test_una_cuenta_eliminada_no_carga(self, gestor):
        identificador = gestor.get_id()
        gestor.soft_delete()
        db.session.commit()

        assert load_user(identificador) is None

    def test_una_cuenta_bloqueada_no_carga(self, gestor):
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        identificador = gestor.get_id()
        gestor.locked_until = utcnow() + timedelta(minutes=15)
        db.session.commit()

        assert load_user(identificador) is None


@pytest.mark.integration
class TestSesionEntrePeticiones:
    """The end-to-end path, with the cached user cleared between requests.

    Clearing ``g._login_user`` is what a real second request does implicitly by
    starting with a fresh application context. Without it the test would pass
    even when ``load_user`` is broken.
    """

    def _forzar_recarga(self):
        from flask import g

        for attr in ('_login_user', '_trip_membership'):
            if hasattr(g, attr):
                delattr(g, attr)

    def test_la_sesion_persiste_en_la_peticion_siguiente(
        self, client, login, gestor, trip
    ):
        respuesta = login(gestor)
        assert respuesta.status_code == 302
        assert '/auth/login' not in respuesta.headers.get('Location', '')

        self._forzar_recarga()

        panel = client.get('/dashboard/')
        assert panel.status_code == 200, (
            'La sesión debe seguir viva en la petición siguiente al login.'
        )

        self._forzar_recarga()

        detalle = client.get(f'/trips/{trip.id}')
        assert detalle.status_code == 200

    def test_tras_cambiar_la_contrasena_hay_que_volver_a_entrar(
        self, client, login, gestor
    ):
        login(gestor)
        self._forzar_recarga()
        assert client.get('/dashboard/').status_code == 200

        from app.services.identity import get_provider

        get_provider('local').change_password(
            str(gestor.id), None, 'Contrasena-Nueva-Test-2026'
        )
        self._forzar_recarga()

        respuesta = client.get('/dashboard/')
        assert respuesta.status_code == 302
        assert '/auth/login' in respuesta.headers.get('Location', '')
