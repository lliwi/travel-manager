"""Second factor: time-based one-time codes.

What these pin is mostly the ways a second factor stops being one. A code that
works twice, a session handed out before the second step, a policy that only
applies to people who happen to sign in again -- each of those leaves the
feature looking present and doing nothing.

And the other half: losing a phone must not mean losing the account. Recovery
codes, an administrator who can clear it, and a CLI command for the day the
locked-out person is the only administrator.
"""
import time

import pyotp
import pytest

from app.services import mfa_service, settings_service


def _codigo(secreto, desplazamiento=0):
    totp = pyotp.TOTP(secreto)
    return totp.at(int(time.time()) + desplazamiento)


@pytest.fixture
def cli(app):
    """A runner for the console commands."""
    return app.test_cli_runner()


@pytest.fixture
def con_mfa(app, gestor, db):
    """A manager with a confirmed second factor, and their secret.

    The last-used step is cleared afterwards. Enrolling spends the current
    window on purpose -- the confirming code was typed on a screen somebody may
    have been watching -- and every test below is about a sign-in later on,
    which in real life is minutes away and in a test is the same millisecond.
    Leaving it set would make these tests about the clock rather than about
    what they say they check.
    """
    alta = mfa_service.iniciar_alta(gestor)
    mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))
    gestor.mfa_ultimo_paso = None
    db.session.commit()
    return gestor, alta['secreto']


@pytest.mark.integration
class TestDarseDeAlta:
    def test_no_queda_activo_hasta_probarlo(self, app, gestor):
        """A secret somebody scanned wrong would otherwise lock them out of
        their own account on the next sign-in."""
        mfa_service.iniciar_alta(gestor)

        assert gestor.mfa_secret_cifrado is not None
        assert gestor.mfa_activo is False

    def test_un_codigo_correcto_lo_activa(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)

        mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        assert gestor.mfa_activo is True

    def test_un_codigo_incorrecto_no(self, app, gestor):
        from app.utils.errors import ValidationError

        mfa_service.iniciar_alta(gestor)

        with pytest.raises(ValidationError):
            mfa_service.confirmar_alta(gestor, '000000')
        assert gestor.mfa_activo is False

    def test_se_entregan_codigos_de_recuperacion(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)

        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        assert len(codigos) == mfa_service.CODIGOS
        assert gestor.mfa_codigos_disponibles == mfa_service.CODIGOS

    def test_el_qr_es_un_svg_en_linea(self, app, gestor):
        """Inline so a secret never becomes a URL that could be shared by
        accident or sit in a proxy cache."""
        alta = mfa_service.iniciar_alta(gestor)

        assert alta['qr'].lstrip().startswith('<?xml') or '<svg' in alta['qr']
        assert 'otpauth://' in alta['uri']


@pytest.mark.security
class TestUnCodigoValeUnaVez:
    """TOTP accepts the same digits for a whole thirty-second window.

    Without remembering which step the last accepted code belonged to, a code
    read over somebody's shoulder is valid again -- which is most of what the
    second factor was for.
    """

    def test_el_mismo_codigo_no_se_acepta_dos_veces(self, app, con_mfa):
        user, secreto = con_mfa
        codigo = _codigo(secreto)

        assert mfa_service.verificar_codigo(user, codigo) is True
        assert mfa_service.verificar_codigo(user, codigo) is False

    def test_tampoco_el_que_se_uso_para_activarlo(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)
        codigo = _codigo(alta['secreto'])
        mfa_service.confirmar_alta(gestor, codigo)

        assert mfa_service.verificar_codigo(gestor, codigo) is False

    def test_uno_anterior_ya_no_sirve(self, app, con_mfa):
        """Replaying a code from a past window is the same attack with an
        older screenshot."""
        user, secreto = con_mfa
        mfa_service.verificar_codigo(user, _codigo(secreto))

        assert mfa_service.verificar_codigo(user, _codigo(secreto, -60)) is False

    def test_se_tolera_un_reloj_algo_desviado(self, app, con_mfa):
        """Phones drift and people type slowly. Refusing a correct code
        because a clock is twenty seconds out teaches people it is broken."""
        user, secreto = con_mfa

        assert mfa_service.verificar_codigo(user, _codigo(secreto, -30)) is True

    def test_una_cadena_que_no_es_un_codigo_no_pasa(self, app, con_mfa):
        user, _ = con_mfa

        assert mfa_service.verificar_codigo(user, 'abcdef') is False
        assert mfa_service.verificar_codigo(user, '') is False


@pytest.mark.security
class TestNoSeGuardaNadaLegible:
    def test_el_secreto_se_guarda_cifrado(self, app, con_mfa):
        """A TOTP secret is a password equivalent: whoever reads it can
        generate valid codes for ever."""
        user, secreto = con_mfa

        assert secreto not in (user.mfa_secret_cifrado or '')
        assert mfa_service.secreto_de(user) == secreto

    def test_los_codigos_de_recuperacion_se_guardan_con_hash(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)
        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        almacenados = ' '.join(c.code_hash for c in gestor.mfa_recovery_codes)
        assert all(codigo not in almacenados for codigo in codigos)


@pytest.mark.integration
class TestCodigosDeRecuperacion:
    def test_uno_sirve_para_entrar(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)
        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        assert mfa_service.consumir_codigo_de_recuperacion(gestor, codigos[0]) is True

    def test_y_solo_una_vez(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)
        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))
        mfa_service.consumir_codigo_de_recuperacion(gestor, codigos[0])

        assert mfa_service.consumir_codigo_de_recuperacion(gestor, codigos[0]) is False

    def test_se_marca_usado_en_vez_de_borrarse(self, app, gestor):
        """«Somebody used a recovery code» is the signal that a phone was lost
        or that somebody else holds them, and it has to survive the event."""
        alta = mfa_service.iniciar_alta(gestor)
        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))
        mfa_service.consumir_codigo_de_recuperacion(gestor, codigos[0])

        assert len(gestor.mfa_recovery_codes) == mfa_service.CODIGOS
        assert gestor.mfa_codigos_disponibles == mfa_service.CODIGOS - 1

    def test_uno_inventado_no_sirve(self, app, con_mfa):
        user, _ = con_mfa

        assert mfa_service.consumir_codigo_de_recuperacion(user, 'AAAAA-AAAAA') is False

    def test_reemitirlos_invalida_los_anteriores(self, app, gestor):
        alta = mfa_service.iniciar_alta(gestor)
        viejos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        mfa_service.emitir_codigos(gestor)

        assert mfa_service.consumir_codigo_de_recuperacion(gestor, viejos[0]) is False

    def test_se_leen_sin_ambiguedad(self):
        """No vowels, no 0/O, no 1/I: the pairs people transcribe wrongly and
        then conclude the code did not work."""
        for _ in range(50):
            codigo = mfa_service._codigo_legible()
            assert not set(codigo) & set('OI01AEU')


@pytest.mark.security
class TestElSegundoPasoOcurreAntesDeLaSesion:
    """Logging somebody in and then asking would mean a valid session for
    somebody who has proved half of what we ask."""

    def test_la_contrasena_sola_no_da_sesion(self, client, con_mfa, password):
        user, _ = con_mfa

        respuesta = client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })

        assert respuesta.status_code == 302
        assert '/auth/mfa' in respuesta.headers['Location']
        assert client.get('/dashboard/').status_code == 302

    def test_con_el_codigo_si(self, client, con_mfa, password):
        user, secreto = con_mfa
        client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })

        respuesta = client.post('/auth/mfa', data={'codigo': _codigo(secreto)})

        assert respuesta.status_code == 302
        assert client.get('/dashboard/').status_code == 200

    def test_un_codigo_equivocado_no_abre_la_sesion(self, client, con_mfa, password):
        user, _ = con_mfa
        client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })

        respuesta = client.post('/auth/mfa', data={'codigo': '000000'})

        assert respuesta.status_code == 401
        assert client.get('/dashboard/').status_code == 302

    def test_sin_haber_pasado_por_la_contrasena_no_hay_segundo_paso(self, client):
        """Otherwise the second factor becomes the only factor."""
        respuesta = client.post('/auth/mfa', data={'codigo': '000000'})

        assert respuesta.status_code == 302
        assert '/auth/login' in respuesta.headers['Location']

    def test_un_codigo_de_recuperacion_tambien_entra(self, app, client, gestor, password):
        alta = mfa_service.iniciar_alta(gestor)
        codigos = mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        client.post('/auth/login', data={
            'username': gestor.username, 'password': password,
        })
        client.post('/auth/mfa', data={'codigo': codigos[0]})

        assert client.get('/dashboard/').status_code == 200

    def test_el_paso_pendiente_caduca(self, app, client, con_mfa, password, monkeypatch):
        """Walking away from an unlocked machine must not leave the second
        factor pending all afternoon."""
        from app.blueprints.auth import routes

        user, secreto = con_mfa
        client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })
        monkeypatch.setattr(routes, 'MFA_PENDIENTE_SEGUNDOS', -1)

        respuesta = client.post('/auth/mfa', data={'codigo': _codigo(secreto)})

        assert '/auth/login' in respuesta.headers['Location']


@pytest.mark.integration
class TestSinSegundoFactorTodoSigueIgual:
    def test_una_cuenta_sin_mfa_entra_como_siempre(self, client, gestor, password):
        respuesta = client.post('/auth/login', data={
            'username': gestor.username, 'password': password,
        })

        assert respuesta.status_code == 302
        assert '/auth/mfa' not in respuesta.headers['Location']
        assert client.get('/dashboard/').status_code == 200


@pytest.mark.security
class TestLaPoliticaDeObligatoriedad:
    def _exigir(self, nivel):
        settings_service.set_value('MFA_OBLIGATORIO', nivel)

    def test_por_defecto_no_se_exige_a_nadie(self, app, admin, gestor):
        assert mfa_service.es_obligatorio(admin) is False
        assert mfa_service.es_obligatorio(gestor) is False

    def test_se_puede_exigir_solo_a_administradores(self, app, admin, gestor, viajero):
        """An administrator can grant themselves anything, so that is the
        account worth protecting first."""
        self._exigir('administradores')

        assert mfa_service.es_obligatorio(admin) is True
        assert mfa_service.es_obligatorio(gestor) is False
        assert mfa_service.es_obligatorio(viajero) is False

    def test_o_a_quien_gestiona(self, app, admin, gestor, viajero):
        self._exigir('gestion')

        assert mfa_service.es_obligatorio(admin) is True
        assert mfa_service.es_obligatorio(gestor) is True
        assert mfa_service.es_obligatorio(viajero) is False

    def test_o_a_todos(self, app, viajero):
        self._exigir('todos')

        assert mfa_service.es_obligatorio(viajero) is True

    def test_un_valor_desconocido_no_exige_nada(self, app, admin):
        """Failing towards «locked out of the application» because somebody
        mistyped a setting would be a poor trade.

        The panel no longer lets anybody type one -- it is a dropdown, and
        ``set_value`` refuses the rest -- so the value is written straight
        into the row: what is tested is a value already stored, from before
        that, or edited by hand in the database.
        """
        from app.extensions import db
        from app.models.settings import SystemSetting

        settings_service.set_value('MFA_OBLIGATORIO', 'ninguno')
        fila = SystemSetting.query.filter_by(clave='MFA_OBLIGATORIO').one()
        fila.valor = {'v': 'sí, claro'}
        db.session.commit()

        assert mfa_service.es_obligatorio(admin) is False

    def test_a_quien_le_falta_se_le_lleva_a_configurarlo(
        self, as_user, gestor, seeded,
    ):
        """Checked on every request, not only at sign-in: the policy changes
        while everybody is already logged in."""
        self._exigir('gestion')

        with as_user(gestor) as client:
            respuesta = client.get('/trips/')

        assert respuesta.status_code == 302
        assert '/auth/mfa/activar' in respuesta.headers['Location']

    def test_quien_ya_lo_tiene_pasa(self, client, con_mfa, password):
        user, secreto = con_mfa
        self._exigir('gestion')

        client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })
        client.post('/auth/mfa', data={'codigo': _codigo(secreto)})

        assert client.get('/trips/').status_code == 200

    def test_cerrar_sesion_sigue_siendo_posible(self, as_user, gestor, seeded):
        """Somebody who cannot finish the setup must not be trapped in a
        session they cannot end."""
        self._exigir('gestion')

        with as_user(gestor) as client:
            assert client.get('/auth/mfa/activar').status_code == 200

    def test_a_la_api_se_le_responde_y_no_se_le_redirige(
        self, as_user, gestor, seeded,
    ):
        """Following a redirect to an HTML form would look like success."""
        self._exigir('gestion')

        with as_user(gestor) as client:
            respuesta = client.get('/api/v1/trips')

        assert respuesta.status_code == 403
        assert respuesta.get_json()['error']['codigo'] == 'mfa_requerido'

    def test_no_se_puede_desactivar_lo_que_se_exige(
        self, client, con_mfa, password,
    ):
        user, secreto = con_mfa
        self._exigir('gestion')
        client.post('/auth/login', data={
            'username': user.username, 'password': password,
        })
        client.post('/auth/mfa', data={'codigo': _codigo(secreto)})

        client.post('/auth/mfa/desactivar')

        assert user.mfa_activo is True


@pytest.mark.integration
class TestPerderElTelefonoNoEsPerderLaCuenta:
    def test_un_administrador_puede_retirarlo(self, as_user, admin, con_mfa):
        user, _ = con_mfa

        with as_user(admin) as client:
            client.post(f'/admin/usuarios/{user.id}/mfa/quitar')

        assert user.mfa_activo is False

    def test_y_corta_las_sesiones_abiertas(self, as_user, admin, con_mfa):
        """Whoever holds one got in with the factor just removed."""
        user, _ = con_mfa
        antes = user.session_epoch

        with as_user(admin) as client:
            client.post(f'/admin/usuarios/{user.id}/mfa/quitar')

        assert user.session_epoch > antes

    def test_quien_no_es_administrador_no_puede(self, as_user, viajero, con_mfa):
        user, _ = con_mfa

        with as_user(viajero) as client:
            respuesta = client.post(f'/admin/usuarios/{user.id}/mfa/quitar')

        assert respuesta.status_code == 403
        assert user.mfa_activo is True

    def test_queda_registrado_quien_lo_hizo(self, as_user, admin, con_mfa):
        """The one action here that removes a protection from somebody else's
        account."""
        from app.models.audit import AuditEvent

        user, _ = con_mfa

        with as_user(admin) as client:
            client.post(f'/admin/usuarios/{user.id}/mfa/quitar')

        evento = AuditEvent.query.filter_by(accion='user.mfa_disabled').first()
        assert evento is not None
        assert evento.metadatos['motivo'] == 'administrador'

    def test_la_orden_de_consola_es_la_salida_de_emergencia(
        self, app, cli, con_mfa,
    ):
        """For the day the locked-out person is the only administrator, which
        is exactly when nobody can log in to fix it."""
        user, _ = con_mfa

        resultado = cli.invoke(args=['mfa-reset', user.username])

        assert resultado.exit_code == 0
        assert user.mfa_activo is False


@pytest.mark.integration
class TestSeAdministraYSeVe:
    def test_el_ajuste_esta_en_el_panel(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'MFA_OBLIGATORIO' in html
        assert 'Seguridad de las cuentas' in html

    def test_el_perfil_ofrece_activarlo(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            html = client.get('/auth/perfil').get_data(as_text=True)

        assert '/auth/mfa/activar' in html

    def test_una_cuenta_del_directorio_tambien_puede(self, app, gestor):
        """The directory checks the password; this is our factor, on top, and
        independent of where the first one was verified."""
        gestor.identity_provider = 'ldap'

        alta = mfa_service.iniciar_alta(gestor)
        mfa_service.confirmar_alta(gestor, _codigo(alta['secreto']))

        assert gestor.mfa_activo is True
