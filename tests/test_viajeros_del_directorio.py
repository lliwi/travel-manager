"""Poner en un viaje a quien nunca ha entrado en la aplicación.

El aprovisionamiento al iniciar sesión ya existía, así que quien entra queda
registrado. El hueco era el otro momento: al asignar viajeros solo se ofrecían
cuentas locales, de modo que a quien nunca había entrado no se le podía añadir
a un viaje. El día que se instala esto, eso es toda la plantilla -- el
directorio es la nómina y esta tabla está vacía.

La regla que ordena lo demás: **buscar no da de alta a nadie**. La cuenta se
crea cuando alguien decide poner a esa persona en un viaje, no cuando aparece
en una lista de resultados; si no, una búsqueda de «gar» llenaría la tabla de
usuarios con media empresa.
"""
import pytest

from app.models.user import User
from app.services.identity import (
    buscar_en_el_directorio,
    dar_de_alta_desde_el_directorio,
)
from tests.test_directorio import _configurar

pytestmark = pytest.mark.integration


class TestBuscarNoDaDeAlta:
    def test_encuentra_a_quien_no_tiene_cuenta(self, app, seeded, directorio):
        _configurar()

        resultados = buscar_en_el_directorio('alopez')

        assert resultados
        record, local = resultados[0]
        assert record.username == 'alopez'
        assert local is None, 'aún no debería existir aquí'

    def test_y_no_la_crea(self, app, seeded, directorio):
        """Una búsqueda de «gar» no puede llenar la tabla de usuarios."""
        _configurar()
        antes = User.query.count()

        buscar_en_el_directorio('alopez')

        assert User.query.count() == antes

    def test_dice_quien_ya_esta(self, app, seeded, directorio):
        """Para poder distinguir «ya está» de «habría que darle de alta» sin
        preguntarlo dos veces."""
        _configurar()
        dar_de_alta_desde_el_directorio('alopez')

        _record, local = buscar_en_el_directorio('alopez')[0]

        assert local is not None

    def test_un_texto_demasiado_corto_no_busca(self, app, seeded, directorio,
                                               as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/viajeros?buscar=al').get_data(as_text=True)

        assert 'al menos tres letras' in html

    def test_un_directorio_caido_no_rompe_la_pantalla(self, app, seeded,
                                                      monkeypatch):
        """Lo que ya está en local se tiene que seguir pudiendo asignar."""
        from app.services.identity import registry

        class _Roto:
            codigo = 'ldap'

            def supports_provisioning(self):
                return True

            def search(self, *a, **k):
                raise RuntimeError('sin ruta al directorio')

        monkeypatch.setattr(registry, 'get_provider', lambda *a, **k: _Roto())

        assert buscar_en_el_directorio('alopez') == []


class TestDarDeAltaAlAsignar:
    def test_crea_la_cuenta(self, app, seeded, directorio):
        _configurar()

        usuario = dar_de_alta_desde_el_directorio('alopez')

        assert usuario.username == 'alopez'
        assert usuario.es_local is False

    def test_con_el_rol_basico_y_no_otro(self, app, seeded, directorio):
        """Nunca un rol elevado: quien da de alta está asignando un viaje, no
        decidiendo permisos."""
        _configurar()

        usuario = dar_de_alta_desde_el_directorio('alopez')

        assert usuario.role_codes == ['usuario']

    def test_pedirlo_dos_veces_no_duplica(self, app, seeded, directorio):
        """Dos filas para una persona parten sus viajes por la mitad."""
        _configurar()

        primero = dar_de_alta_desde_el_directorio('alopez')
        segundo = dar_de_alta_desde_el_directorio('alopez')

        assert primero.id == segundo.id
        assert User.query.filter_by(username='alopez').count() == 1

    def test_a_quien_el_directorio_ya_no_conoce_se_le_dice(self, app, seeded,
                                                           directorio):
        from app.utils.errors import ValidationError

        _configurar()

        with pytest.raises(ValidationError) as exc:
            dar_de_alta_desde_el_directorio('nadie-con-este-nombre')

        assert 'Vuelva a buscarla' in exc.value.mensaje


class TestDesdeLaPantallaDeViajeros:
    def test_la_busqueda_ofrece_darle_de_alta(self, app, seeded, directorio,
                                              as_user, gestor, trip):
        _configurar()

        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/viajeros?buscar=alopez').get_data(as_text=True)

        assert 'Ana López' in html
        assert 'Dar de alta y asignar' in html

    def test_y_al_pulsarlo_queda_en_el_viaje(self, app, seeded, directorio,
                                             as_user, gestor, trip):
        _configurar()

        with as_user(gestor) as client:
            client.post(f'/trips/{trip.id}/viajeros/del-directorio',
                        data={'csrf_token': 'x', 'identificador': 'alopez'},
                        follow_redirects=True)

        usuario = User.query.filter_by(username='alopez').first()

        assert usuario is not None
        assert trip.traveler_for(usuario.id) is not None

    def test_sin_identificador_no_hace_nada(self, app, seeded, directorio,
                                            as_user, gestor, trip):
        antes = User.query.count()

        with as_user(gestor) as client:
            client.post(f'/trips/{trip.id}/viajeros/del-directorio',
                        data={'csrf_token': 'x'}, follow_redirects=True)

        assert User.query.count() == antes

    def test_quien_no_gestiona_viajeros_no_puede(self, app, seeded, directorio,
                                                 as_user, viajero, trip):
        with as_user(viajero) as client:
            respuesta = client.post(
                f'/trips/{trip.id}/viajeros/del-directorio',
                data={'csrf_token': 'x', 'identificador': 'alopez'})

        assert respuesta.status_code in (403, 404)
        assert User.query.filter_by(username='alopez').first() is None


class TestQuienSeBorroSePuedeVolverAAsignar:
    """Found in production: after deleting a directory account, the search
    said «ya está dado de alta» and the dropdown -- rightly -- left the
    deleted account out, so there was no way to put that person on a trip.
    """

    @pytest.fixture
    def borrada(self, app, seeded, directorio, admin):
        from app.models.user import Role
        from app.services import user_service

        _configurar()
        usuario = dar_de_alta_desde_el_directorio('alopez')
        usuario.roles.append(Role.get('administrador'))
        from app.extensions import db

        db.session.commit()
        user_service.delete_user(admin, usuario)
        return usuario

    def test_la_busqueda_ya_no_la_da_por_dada_de_alta(self, borrada):
        (_record, local), = buscar_en_el_directorio('alopez')

        assert local is None

    def test_dar_de_alta_reactiva_la_misma_cuenta(self, borrada):
        """Not a second row: the username is unique and the history is hers."""
        from app.models.enums import UserStatus

        usuario = dar_de_alta_desde_el_directorio('alopez')

        assert usuario.id == borrada.id
        assert not usuario.is_deleted
        assert usuario.estado is UserStatus.ACTIVO
        assert User.query.filter_by(username='alopez').count() == 1

    def test_vuelve_con_el_rol_basico(self, borrada):
        """A manager assigning a trip must not bring back an administrator."""
        usuario = dar_de_alta_desde_el_directorio('alopez')

        assert usuario.role_codes == ['usuario']

    def test_queda_auditado_con_lo_que_tenia(self, borrada):
        from app.models.audit import AuditEvent

        dar_de_alta_desde_el_directorio('alopez')

        evento = AuditEvent.query.filter_by(
            accion='user.reactivated_from_directory').one()
        assert 'administrador' in evento.metadatos['roles_anteriores']

    def test_desde_la_pantalla_queda_en_el_viaje(self, borrada, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/viajeros?buscar=alopez').get_data(as_text=True)
            assert 'Dar de alta y asignar' in html
            client.post(f'/trips/{trip.id}/viajeros/del-directorio',
                        data={'csrf_token': 'x', 'identificador': 'alopez'})

        assert trip.traveler_for(borrada.id) is not None


class TestUnaCuentaDesactivadaNoLaReactivaUnGestor:
    """Deactivated, not deleted: an administrator's decision stands."""

    @pytest.fixture
    def desactivada(self, app, seeded, directorio):
        from app.extensions import db
        from app.models.enums import UserStatus

        _configurar()
        usuario = dar_de_alta_desde_el_directorio('alopez')
        usuario.estado = UserStatus.INACTIVO
        db.session.commit()
        return usuario

    def test_asignarla_se_niega_y_dice_por_que(self, desactivada):
        from app.utils.errors import ValidationError

        with pytest.raises(ValidationError, match='desactivada'):
            dar_de_alta_desde_el_directorio('alopez')

    def test_la_busqueda_lo_dice(self, desactivada, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/viajeros?buscar=alopez').get_data(as_text=True)

        assert 'cuenta desactivada' in html
        assert 'Dar de alta y asignar' not in html
