"""Signing in with the corporate directory (Active Directory / LDAP).

The seam for this was built in phase 1 and left empty: ``IdentityProvider``,
``users.identity_provider``, ``users.external_id``, a nullable
``password_hash`` and ``role_group_mappings``. This is the implementation, and
what these tests pin is mostly the things that would make it dangerous rather
than merely broken.

Two properties matter more than the rest:

- **It does not replace local accounts.** Turning the directory on disables
  nothing. An administrator who cannot reach the directory can still log in,
  which is the difference between a bad afternoon and being locked out of the
  application that manages the trips.
- **Authorisation stays ours.** The directory says who somebody is. A
  provisioned account gets the plain ``usuario`` role and everything above that
  is granted here, by a person.

Nothing here opens a socket. ldap3's mock strategy answers from an in-memory
tree, so these tests say something about the code rather than about whether a
directory happened to be reachable.
"""
import json

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from app.services import settings_service
from app.services.identity import ldap as ldap_module
from app.services.identity.ldap import LDAPIdentityProvider

RAIZ = 'dc=corp,dc=test'
OU = f'ou=usuarios,{RAIZ}'
GRUPO = f'cn=viajes,{OU}'
SERVICIO = f'cn=svc,ou=servicios,{RAIZ}'

ANA = f'uid=alopez,{OU}'
DANI = f'uid=dperez,{OU}'

CLAVE = 'Directorio.2026'

#: La cadena completa, como la guarda un directorio real. El servidor simulado
#: de ldap3 compara las clases literalmente y no sabe que «inetOrgPerson» es un
#: «person», así que sin esto el filtro no encuentra a nadie -- y el fallo se
#: parecería a un error nuestro en vez de a una diferencia del simulador.
PERSONA = ['top', 'person', 'organizationalPerson', 'inetOrgPerson']


def _configurar(ruta=OU, habilitado=True, atributo='uid'):
    for clave, valor in (
        ('LDAP_HABILITADO', habilitado),
        ('LDAP_SERVIDOR', 'directorio.test'),
        ('LDAP_PUERTO', 389),
        ('LDAP_USUARIO', SERVICIO),
        ('LDAP_CONTRASENA', 'Consulta.2026'),
        ('LDAP_RUTA', ruta),
        ('LDAP_ATRIBUTO_USUARIO', atributo),
    ):
        settings_service.set_value(clave, valor)


def _arbol(conexion):
    """Seed the in-memory directory: an OU with two people and a group."""
    conexion.strategy.add_entry(SERVICIO, {
        'objectClass': PERSONA, 'cn': 'svc', 'sn': 'Servicio',
        'userPassword': 'Consulta.2026',
    })
    conexion.strategy.add_entry(OU, {'objectClass': ['organizationalUnit'], 'ou': 'usuarios'})
    conexion.strategy.add_entry(ANA, {
        'objectClass': PERSONA, 'uid': 'alopez', 'cn': 'Ana López',
        'givenName': 'Ana', 'sn': 'López', 'mail': 'alopez@corp.test',
        'title': 'Responsable', 'userPassword': CLAVE,
    })
    conexion.strategy.add_entry(DANI, {
        'objectClass': PERSONA, 'uid': 'dperez', 'cn': 'Daniel Pérez',
        'givenName': 'Daniel', 'sn': 'Pérez', 'mail': 'dperez@corp.test',
        'userPassword': CLAVE,
    })
    conexion.strategy.add_entry(GRUPO, {
        'objectClass': ['groupOfNames'], 'cn': 'viajes', 'member': [ANA],
    })


@pytest.fixture
def directorio(monkeypatch):
    """Replace the real connection with ldap3's in-memory server."""
    servidor = Server('directorio.test', get_info=None)

    def _conectar(self, usuario=None, contrasena=None):
        conf = ldap_module._conf()
        usuario = usuario if usuario is not None else conf['usuario']
        contrasena = contrasena if contrasena is not None else conf['contrasena']

        conexion = Connection(
            servidor, user=usuario, password=contrasena,
            client_strategy=MOCK_SYNC,
        )
        # Por conexión: cada Connection en MOCK_SYNC tiene su propio árbol en
        # memoria, así que sembrarlo una sola vez dejaría vacías a las demás.
        _arbol(conexion)
        return conexion if conexion.bind() else None

    monkeypatch.setattr(LDAPIdentityProvider, '_conectar', _conectar)
    return LDAPIdentityProvider()


@pytest.mark.integration
class TestNoHaceFaltaConfigurarloParaQueTodoSigaIgual:
    def test_apagado_no_esta_configurado(self, app, seeded):
        _configurar(habilitado=False)

        assert ldap_module.esta_configurado() is False

    def test_a_medio_rellenar_tampoco(self, app, seeded):
        """A half-filled form would fail every login with a message about the
        directory, which is a confusing way to learn the page was saved early."""
        _configurar()
        settings_service.set_value('LDAP_RUTA', '')

        assert ldap_module.esta_configurado() is False

    def test_sin_configurar_no_se_intenta_autenticar(self, app, seeded):
        _configurar(habilitado=False)

        assert LDAPIdentityProvider().authenticate('alopez', CLAVE).exito is False


@pytest.mark.integration
class TestEntrarConElDirectorio:
    def test_credencial_correcta(self, app, seeded, directorio):
        _configurar()

        resultado = directorio.authenticate('alopez', CLAVE)

        assert resultado.exito is True
        assert resultado.record.username == 'alopez'
        assert resultado.record.email == 'alopez@corp.test'
        assert resultado.record.nombre == 'Ana'
        assert resultado.record.apellidos == 'López'

    def test_credencial_incorrecta(self, app, seeded, directorio):
        _configurar()

        assert directorio.authenticate('alopez', 'otra').exito is False

    def test_quien_no_existe(self, app, seeded, directorio):
        _configurar()

        assert directorio.authenticate('nadie', CLAVE).exito is False

    def test_una_contrasena_vacia_no_es_un_bind_anonimo(
        self, app, seeded, directorio,
    ):
        """Many directories accept an empty password as an anonymous bind,
        which would let anybody in as anybody."""
        _configurar()

        resultado = directorio.authenticate('alopez', '')

        assert resultado.exito is False
        assert resultado.motivo == 'credenciales_vacias'


@pytest.mark.security
class TestElNombreDeUsuarioNoEsUnFiltro:
    def test_un_comodin_no_encuentra_a_cualquiera(self, app, seeded, directorio):
        """Unescaped, «*» matches everybody and the first person in the
        directory gets looked up instead: the LDAP shape of an injection."""
        _configurar()

        assert directorio.authenticate('*', CLAVE).exito is False

    def test_se_escapan_los_metacaracteres(self):
        assert ldap_module._escapar('a*b(c)d') == 'a\\2ab\\28c\\29d'


@pytest.mark.integration
class TestLaRutaPuedeSerUnaOuOUnGrupo:
    def test_con_una_ou_entran_los_de_dentro(self, app, seeded, directorio):
        _configurar(ruta=OU)

        assert directorio.authenticate('alopez', CLAVE).exito is True
        assert directorio.authenticate('dperez', CLAVE).exito is True

    def test_con_un_grupo_solo_entran_sus_miembros(self, app, seeded, directorio):
        """The point of being able to name a group: the OU holds everybody in
        the company and only some of them should reach this application."""
        _configurar(ruta=GRUPO)

        assert directorio.authenticate('alopez', CLAVE).exito is True
        assert directorio.authenticate('dperez', CLAVE).exito is False

    def test_la_raiz_se_deduce_de_la_ruta(self):
        """Members of a group live wherever they live, so the search widens to
        the domain -- which is already inside the path somebody typed."""
        assert ldap_module._raiz_de(GRUPO) == RAIZ
        assert ldap_module._raiz_de('OU=X,OU=Y,DC=corp,DC=local') == 'DC=corp,DC=local'


@pytest.mark.integration
class TestElDirectorioNoSustituyeALoLocal:
    """Turning it on disables nothing. Both work at the same time."""

    def test_una_cuenta_local_sigue_entrando(self, app, seeded, directorio, gestor):
        from app.services.identity import authenticate

        _configurar()

        assert authenticate(gestor.username, 'Contrasena-Test-2026') is not None

    def test_cada_cuenta_sabe_de_donde_viene(self, app, seeded, gestor):
        from app.services.identity.registry import _provider_for

        _configurar()

        assert _provider_for(gestor.username) == 'local'

    def test_un_nombre_desconocido_va_al_directorio(self, app, seeded):
        """Only the directory can create an account for somebody who has never
        logged in, so that is where an unknown name has to go."""
        from app.services.identity.registry import _provider_for

        _configurar()

        assert _provider_for('alguien-nuevo') == 'ldap'

    def test_sin_directorio_un_desconocido_falla_como_siempre(self, app, seeded):
        from app.services.identity.registry import _provider_for

        _configurar(habilitado=False)

        assert _provider_for('alguien-nuevo') is None


@pytest.mark.integration
class TestLaPrimeraVezSeCreaLaCuenta:
    def test_se_aprovisiona_con_el_rol_basico(self, app, seeded, directorio):
        """The directory says who somebody is; it does not say what they may
        do. Anything above «usuario» is granted here, by a person."""
        from app.services.identity import authenticate

        _configurar()

        user = authenticate('alopez', CLAVE)

        assert user.role_codes == ['usuario']
        assert user.identity_provider == 'ldap'
        assert user.username == 'alopez'

    def test_no_se_le_pone_contrasena_local(self, app, seeded, directorio):
        from app.services.identity import authenticate

        _configurar()

        assert authenticate('alopez', CLAVE).password_hash is None

    def test_el_segundo_inicio_no_duplica_la_cuenta(self, app, seeded, directorio):
        from app.models.user import User
        from app.services.identity import authenticate

        _configurar()

        primero = authenticate('alopez', CLAVE)
        segundo = authenticate('alopez', CLAVE)

        assert primero.id == segundo.id
        assert User.query.filter_by(identity_provider='ldap').count() == 1

    def test_el_nombre_de_usuario_es_el_del_directorio(
        self, app, seeded, directorio,
    ):
        """It fell back to the display name once, because the configured login
        attribute was not among the ones requested. «Ana López» cannot be typed
        into a login form."""
        from app.services.identity import authenticate

        _configurar()

        assert authenticate('alopez', CLAVE).username == 'alopez'


@pytest.mark.security
class TestLosRolesSonNuestros:
    def test_un_rol_concedido_a_mano_sobrevive_al_siguiente_inicio(
        self, app, seeded, directorio,
    ):
        """Without group mappings configured, a later login must not quietly
        take back what an administrator granted."""
        from app.extensions import db
        from app.models.user import Role
        from app.services.identity import authenticate

        _configurar()
        user = authenticate('alopez', CLAVE)
        user.roles.append(Role.get('gestor'))
        db.session.commit()

        de_nuevo = authenticate('alopez', CLAVE)

        assert 'gestor' in de_nuevo.role_codes

    def test_no_se_puede_poner_contrasena_local_a_una_cuenta_del_directorio(
        self, app, seeded, directorio, admin,
    ):
        """It would never be consulted -- the login path routes by provider --
        and somebody would rely on it the day the directory is down."""
        from app.services import user_service
        from app.services.identity import authenticate
        from app.utils.errors import ValidationError

        _configurar()
        user = authenticate('alopez', CLAVE)

        with pytest.raises(ValidationError):
            user_service.update_user(
                actor=admin, user=user, password='OtraClave.2026!',
            )


@pytest.mark.integration
class TestSeAdministraDesdeAjustes:
    def test_el_bloque_aparece(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'Directorio corporativo' in html
        assert 'LDAP_RUTA' in html

    def test_hay_un_boton_para_probar_la_conexion(self, as_user, admin, seeded):
        """Otherwise the only way to find out is somebody failing to log in."""
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert '/admin/directorio/probar' in html

    def test_la_prueba_dice_lo_que_pasa(self, as_user, admin, seeded, directorio):
        _configurar()

        with as_user(admin) as client:
            html = client.get('/admin/directorio/probar',
                              follow_redirects=True).get_data(as_text=True)

        assert 'Conexión correcta' in html

    def test_sin_configurar_la_prueba_lo_dice(self, as_user, admin, seeded):
        _configurar(habilitado=False)

        with as_user(admin) as client:
            html = client.get('/admin/directorio/probar',
                              follow_redirects=True).get_data(as_text=True)

        assert 'Faltan datos' in html

    def test_solo_un_administrador_la_lanza(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            assert client.get('/admin/directorio/probar').status_code == 403

    def test_se_ve_de_donde_viene_cada_cuenta(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/usuarios').get_data(as_text=True)

        assert 'Origen' in html


@pytest.mark.security
class TestLaContrasenaDeConsultaNoSeEscapa:
    def test_se_guarda_cifrada(self, app, seeded):
        from app.models.settings import SystemSetting

        settings_service.set_value('LDAP_CONTRASENA', 'Consulta.Secreta')

        fila = SystemSetting.query.filter_by(clave='LDAP_CONTRASENA').first()
        assert 'Consulta.Secreta' not in str(fila.valor)
        assert settings_service.get('LDAP_CONTRASENA') == 'Consulta.Secreta'

    def test_no_se_devuelve_a_la_pantalla(self, as_user, admin, seeded):
        settings_service.set_value('LDAP_CONTRASENA', 'Consulta.Secreta')

        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'Consulta.Secreta' not in html

    def test_un_login_fallido_no_la_registra(self, app, seeded, directorio):
        from app.models.audit import AuditEvent
        from app.services.identity import authenticate
        from app.utils.errors import AuthenticationFailed

        _configurar()
        with pytest.raises(AuthenticationFailed):
            authenticate('alopez', 'mal')

        eventos = AuditEvent.query.filter_by(accion='auth.login_failed').all()
        volcado = json.dumps([e.metadatos for e in eventos], default=str)
        assert 'Consulta.2026' not in volcado
        assert 'mal' not in volcado.replace('normal', '')
