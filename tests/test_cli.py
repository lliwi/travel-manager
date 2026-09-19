"""Operational CLI commands.

``create-admin`` and ``create-user`` can overwrite an existing account, which
replaces its password and roles and ends its sessions. These tests pin down
when that happens and when it is refused.
"""
import pytest

from app.extensions import db
from app.models.audit import AuditEvent
from app.models.enums import RoleCode, UserStatus
from app.models.user import User

CONTRASENA = 'Contrasena-Test-2026'
OTRA = 'Otra-Contrasena-Test-2026'


@pytest.fixture
def runner(app):
    """A CLI runner bound to the application."""
    return app.test_cli_runner()


def _invoke(runner, *args):
    from app.cli import create_admin

    return runner.invoke(create_admin, list(args))


@pytest.mark.unit
class TestAltaDeCuentas:
    def test_se_crea_una_cuenta_nueva(self, runner, seeded):
        resultado = _invoke(
            runner,
            '--username', 'nueva', '--email', 'nueva@corp.test',
            '--nombre', 'Nueva', '--password', CONTRASENA,
        )
        assert resultado.exit_code == 0, resultado.output
        assert 'creado' in resultado.output

        user = User.query.filter_by(username='nueva').first()
        assert user is not None
        assert user.has_role(RoleCode.ADMINISTRADOR)
        assert user.estado is UserStatus.ACTIVO

    def test_el_correo_se_guarda_en_minusculas(self, runner, seeded):
        _invoke(
            runner,
            '--username', 'mayus', '--email', 'MAYUS@Corp.TEST',
            '--nombre', 'Mayús', '--password', CONTRASENA,
        )
        assert User.query.filter_by(username='mayus').first().email == 'mayus@corp.test'

    def test_una_contrasena_debil_se_rechaza_antes_de_crear(self, runner, seeded):
        resultado = _invoke(
            runner,
            '--username', 'debil', '--email', 'debil@corp.test',
            '--nombre', 'Débil', '--password', 'password',
        )
        assert resultado.exit_code != 0
        assert 'no cumple los requisitos' in resultado.output
        assert User.query.filter_by(username='debil').first() is None


@pytest.mark.unit
class TestSobrescritura:
    """Overwriting replaces the password and the roles, and ends sessions."""

    @pytest.fixture
    def existente(self, runner, seeded):
        _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Original', '--password', CONTRASENA,
        )
        return User.query.filter_by(username='repetido').first()

    def test_sin_overwrite_no_se_toca_la_cuenta(self, runner, existente):
        hash_original = existente.password_hash

        resultado = _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Otro', '--password', OTRA,
        )

        assert resultado.exit_code != 0
        assert 'Ya existe la cuenta' in resultado.output
        assert '--overwrite' in resultado.output

        db.session.refresh(existente)
        assert existente.password_hash == hash_original, (
            'Sin confirmación explícita la contraseña no debe cambiar.'
        )
        assert existente.nombre == 'Original'

    def test_con_overwrite_se_reemplaza(self, runner, existente):
        hash_original = existente.password_hash
        epoca_original = existente.session_epoch

        resultado = _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Cambiado', '--password', OTRA, '--overwrite',
        )

        assert resultado.exit_code == 0, resultado.output
        assert 'sobrescrito' in resultado.output

        db.session.refresh(existente)
        assert existente.nombre == 'Cambiado'
        assert existente.password_hash != hash_original
        assert existente.session_epoch > epoca_original, (
            'Cambiar la contraseña debe invalidar las sesiones abiertas.'
        )

    def test_la_nueva_contrasena_es_la_valida(self, runner, existente):
        from app.utils.crypto import verify_password

        _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Cambiado', '--password', OTRA, '--overwrite',
        )
        db.session.refresh(existente)

        assert verify_password(existente.password_hash, OTRA)[0]
        assert not verify_password(existente.password_hash, CONTRASENA)[0]

    def test_se_reemplazan_los_roles(self, runner, existente, seeded):
        from app.cli import create_user

        assert existente.role_codes == ['administrador']

        resultado = runner.invoke(create_user, [
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Cambiado', '--rol', 'gestor',
            '--password', OTRA, '--overwrite',
        ])

        assert resultado.exit_code == 0, resultado.output
        db.session.refresh(existente)
        assert existente.role_codes == ['gestor'], (
            'Los roles se reemplazan, no se acumulan.'
        )

    def test_se_reactiva_una_cuenta_eliminada(self, runner, existente):
        existente.soft_delete()
        existente.estado = UserStatus.INACTIVO
        db.session.commit()

        resultado = _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Recuperado', '--password', OTRA, '--overwrite',
        )

        assert resultado.exit_code == 0, resultado.output
        assert 'reactivar' in resultado.output

        db.session.refresh(existente)
        assert not existente.is_deleted
        assert existente.estado is UserStatus.ACTIVO

    def test_se_levanta_el_bloqueo_por_intentos_fallidos(self, runner, existente):
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        existente.failed_login_count = 5
        existente.locked_until = utcnow() + timedelta(hours=1)
        db.session.commit()

        _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Recuperado', '--password', OTRA, '--overwrite',
        )

        db.session.refresh(existente)
        assert existente.failed_login_count == 0
        assert existente.locked_until is None
        assert not existente.is_locked

    def test_la_sobrescritura_queda_auditada(self, runner, existente):
        _invoke(
            runner,
            '--username', 'repetido', '--email', 'repetido@corp.test',
            '--nombre', 'Cambiado', '--password', OTRA, '--overwrite',
        )

        evento = AuditEvent.query.filter_by(accion='user.overwritten').first()
        assert evento is not None, 'Sobrescribir una cuenta debe auditarse.'
        assert evento.metadatos['username'] == 'repetido'
        assert 'antes' in evento.metadatos and 'despues' in evento.metadatos

    def test_se_encuentra_la_cuenta_por_el_correo(self, runner, existente):
        """A different username with the same email is still the same account."""
        resultado = _invoke(
            runner,
            '--username', 'renombrado', '--email', 'repetido@corp.test',
            '--nombre', 'Renombrado', '--password', OTRA, '--overwrite',
        )

        assert resultado.exit_code == 0, resultado.output
        db.session.refresh(existente)
        assert existente.username == 'renombrado'
        assert User.query.count() == 1, 'No debe crearse una segunda cuenta.'


@pytest.mark.unit
class TestAmbiguedad:
    """Two identifiers pointing at two accounts is refused, not guessed."""

    def test_se_niega_cuando_apuntan_a_cuentas_distintas(self, runner, seeded):
        for n in ('uno', 'dos'):
            _invoke(
                runner,
                '--username', n, '--email', f'{n}@corp.test',
                '--nombre', n.capitalize(), '--password', CONTRASENA,
            )
        assert User.query.count() == 2

        resultado = _invoke(
            runner,
            '--username', 'uno', '--email', 'dos@corp.test',
            '--nombre', 'Confuso', '--password', OTRA, '--overwrite',
        )

        assert resultado.exit_code != 0
        assert 'cuentas distintas' in resultado.output

        # Neither account may have been touched.
        assert User.query.filter_by(username='uno').first().nombre == 'Uno'
        assert User.query.filter_by(username='dos').first().nombre == 'Dos'
        assert User.query.count() == 2


@pytest.mark.unit
class TestRetencion:
    """Erasing an original is irreversible, so it needs a deliberate lever.

    The nightly task only ever reports candidates -- by design, since how long
    to keep a document is an organisational decision. But without a command to
    run the destructive pass, retention could never actually be applied.
    """

    def _documento_caducado(self, gestor, trip, booking_pdf):
        import io
        from datetime import timedelta

        from werkzeug.datastructures import FileStorage

        from app.models.enums import DocumentType
        from app.services import document_service
        from app.utils.timeutil import utcnow

        storage = FileStorage(
            stream=io.BytesIO(booking_pdf), filename='viejo.pdf',
            content_type='application/pdf',
        )
        documento = document_service.upload(
            gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
        )
        db.session.commit()

        documento.objeto_storage = 'docs/viejo.pdf'
        documento.retencion_hasta = utcnow() - timedelta(days=1)
        db.session.commit()
        return documento

    def _invoke(self, runner, *args, **kwargs):
        from app.cli import apply_retention

        return runner.invoke(apply_retention, list(args), **kwargs)

    def test_sin_execute_solo_informa(self, runner, gestor, trip, booking_pdf):
        documento = self._documento_caducado(gestor, trip, booking_pdf)

        resultado = self._invoke(runner)

        assert resultado.exit_code == 0
        assert 'superado su plazo' in resultado.output
        db.session.refresh(documento)
        assert documento.objeto_storage is not None, (
            'Una simulación no puede borrar nada.'
        )

    def test_sin_candidatos_lo_dice(self, runner, gestor, trip, booking_pdf):
        resultado = self._invoke(runner)

        assert resultado.exit_code == 0
        assert 'Ningún documento' in resultado.output

    def test_con_execute_y_confirmacion_borra(
        self, runner, gestor, trip, booking_pdf
    ):
        documento = self._documento_caducado(gestor, trip, booking_pdf)

        resultado = self._invoke(runner, '--execute', '--yes')

        assert resultado.exit_code == 0
        db.session.refresh(documento)
        assert documento.objeto_storage is None

    def test_la_ficha_y_el_hash_sobreviven_al_borrado(
        self, runner, gestor, trip, booking_pdf
    ):
        """What is erased is the content, not the evidence that it existed."""
        documento = self._documento_caducado(gestor, trip, booking_pdf)
        hash_original = documento.hash_sha256

        self._invoke(runner, '--execute', '--yes')

        db.session.refresh(documento)
        assert documento.hash_sha256 == hash_original
        assert not documento.is_deleted
        assert AuditEvent.query.filter_by(
            accion='document.purged_by_retention'
        ).count() == 1

    def test_se_aborta_si_no_se_confirma(self, runner, gestor, trip, booking_pdf):
        documento = self._documento_caducado(gestor, trip, booking_pdf)

        resultado = self._invoke(runner, '--execute', input='n\n')

        assert resultado.exit_code != 0
        db.session.refresh(documento)
        assert documento.objeto_storage is not None


@pytest.mark.unit
class TestTareasProgramadas:
    """A verifiable audit trail that nobody verifies is one nobody trusts."""

    def test_la_cadena_de_auditoria_se_verifica_sola(self):
        from app.tasks.celery_app import celery

        tareas = {
            cfg['task'] for cfg in celery.conf.beat_schedule.values()
        }

        assert 'app.tasks.maintenance.verify_audit_chain' in tareas, (
            'La tarea existía pero nada la ejecutaba: la manipulación solo '
            'cuenta como detectada si algo mira.'
        )

    def test_la_retencion_programada_no_borra_por_su_cuenta(self):
        """The nightly pass reports; a person runs the destructive one."""
        from app.tasks.celery_app import celery

        entrada = next(
            cfg for cfg in celery.conf.beat_schedule.values()
            if cfg['task'] == 'app.tasks.maintenance.apply_retention'
        )

        kwargs = entrada.get('kwargs') or {}

        assert kwargs.get('dry_run', True), (
            'Programar el borrado real convierte una decisión de la '
            'organización en un valor por defecto.'
        )
