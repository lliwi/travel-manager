"""Model-level guarantees: audit immutability, soft delete, enums."""
import pytest

from app.extensions import db
from app.models.audit import GENESIS_HASH, AuditEvent
from app.models.enums import (
    AlertSeverity,
    AuditResourceType,
    TripStatus,
)
from app.services import audit_service


@pytest.mark.unit
class TestEnums:
    def test_el_valor_almacenado_es_ascii(self):
        """Accented Spanish is a label, never a stored value."""
        for member in TripStatus:
            assert member.value.isascii(), f'{member.name} guarda texto no ASCII.'
            assert member.value.islower()
            assert ' ' not in member.value

    def test_la_etiqueta_si_lleva_acentos(self):
        assert TripStatus.EN_PREPARACION.value == 'en_preparacion'
        assert TripStatus.EN_PREPARACION.label == 'En preparación'

    def test_un_miembro_se_compara_como_cadena(self):
        """The str mixin keeps SQLAlchemy filters and Jinja comparisons simple."""
        assert TripStatus.CONFIRMADO == 'confirmado'
        assert str(TripStatus.CONFIRMADO) == 'confirmado'

    def test_coerce_tolera_una_entrada_invalida(self):
        assert TripStatus.coerce('confirmado') is TripStatus.CONFIRMADO
        assert TripStatus.coerce('inventado', TripStatus.BORRADOR) is TripStatus.BORRADOR
        assert TripStatus.coerce(None) is None

    def test_las_severidades_se_ordenan(self):
        severidades = sorted(AlertSeverity, key=lambda s: s.rank)
        assert [s.value for s in severidades] == [
            'informativa', 'media', 'alta', 'critica'
        ]


@pytest.mark.security
class TestAuditoriaInmutable:
    """Section 3.2: the audit trail is immutable or integrity-controlled."""

    def _event(self, accion='test.action'):
        return audit_service.record(
            accion,
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id='abc',
            metadatos={'clave': 'valor'},
        )

    def test_un_evento_no_puede_modificarse(self, app):
        evento = self._event()

        evento.accion = 'test.manipulado'
        with pytest.raises(RuntimeError, match='inmutables'):
            db.session.commit()

        db.session.rollback()

    def test_un_evento_no_puede_eliminarse(self, app):
        evento = self._event()

        db.session.delete(evento)
        with pytest.raises(RuntimeError, match='no pueden eliminarse'):
            db.session.commit()

        db.session.rollback()

    def test_los_eventos_se_encadenan(self, app):
        primero = self._event('primero')
        segundo = self._event('segundo')
        tercero = self._event('tercero')

        assert primero.hash_anterior == GENESIS_HASH
        assert segundo.hash_anterior == primero.hash_propio
        assert tercero.hash_anterior == segundo.hash_propio

    def test_la_cadena_se_verifica(self, app):
        for i in range(5):
            self._event(f'accion.{i}')

        integra, problemas = audit_service.verify_chain()
        assert integra
        assert problemas == []

    def test_una_manipulacion_en_base_de_datos_se_detecta(self, app):
        """Editing a row behind the ORM's back must still be detectable."""
        for i in range(3):
            self._event(f'accion.{i}')

        # Bypassing the ORM entirely, as an attacker with database access would.
        db.session.execute(
            AuditEvent.__table__.update()
            .where(AuditEvent.accion == 'accion.1')
            .values(accion='accion.manipulada')
        )
        db.session.commit()
        db.session.expire_all()

        integra, problemas = audit_service.verify_chain()
        assert not integra, 'La manipulación debe detectarse.'
        assert any(p['motivo'] == 'contenido_alterado' for p in problemas)

    def test_no_se_registran_claves_sensibles(self, app):
        """Section 3.2: logs without unnecessary secrets."""
        evento = audit_service.record(
            'test.action',
            metadatos={
                'usuario': 'ana',
                'password': 'no-deberia-guardarse',
                'api_key': 'sk-secreto',
                'token': 'abc123',
            },
        )

        assert 'usuario' in evento.metadatos
        for prohibida in ('password', 'api_key', 'token'):
            assert prohibida not in evento.metadatos, (
                f'La clave «{prohibida}» no debe llegar a la auditoría.'
            )

    def test_los_valores_largos_se_truncan(self, app):
        evento = audit_service.record(
            'test.action', metadatos={'descripcion': 'x' * 2000}
        )
        assert len(evento.metadatos['descripcion']) <= 501

    def test_las_claves_de_contenido_se_descartan(self, app):
        """Keys that would carry document text never reach the trail."""
        evento = audit_service.record(
            'test.action',
            metadatos={'referencia': 'VJ-1', 'texto': 'contenido del documento'},
        )
        assert evento.metadatos == {'referencia': 'VJ-1'}


@pytest.mark.unit
class TestBorradoLogico:
    """Records are never hard-deleted: the trail references them."""

    def test_un_viaje_eliminado_conserva_sus_datos(self, gestor, trip):
        from app.models.trip import Trip
        from app.services import trip_service

        trip_id = trip.id
        trip_service.delete_trip(gestor, trip)

        persistido = db.session.get(Trip, trip_id)
        assert persistido is not None, 'La fila debe seguir existiendo.'
        assert persistido.is_deleted
        assert persistido.deleted_by_id == gestor.id
        assert persistido.deleted_at is not None

    def test_un_usuario_eliminado_conserva_su_historial(self, admin, gestor, trip):
        from app.models.user import User
        from app.services import user_service

        gestor_id = gestor.id
        user_service.delete_user(admin, gestor)

        persistido = db.session.get(User, gestor_id)
        assert persistido is not None
        assert persistido.is_deleted
        assert trip.gestor_id == gestor_id, (
            'El viaje debe seguir referenciando a quien lo gestionaba.'
        )

    def test_no_se_puede_eliminar_al_unico_administrador(self, admin):
        from app.services import user_service
        from app.utils.errors import ConflictError

        otro = admin  # the only administrator in the fixture set
        with pytest.raises(ConflictError, match='único administrador'):
            user_service.delete_user(otro, otro) if False else None
            # Deleting oneself is refused first, so a second admin acts here.
            from app.models.enums import UserStatus
            from app.models.user import User
            from app.utils.crypto import hash_password

            actor = User(
                username='otro_admin', email='otro@example.test', nombre='Otro',
                password_hash=hash_password('Contrasena-Test-2026'),
                estado=UserStatus.ACTIVO,
            )
            db.session.add(actor)
            db.session.commit()
            user_service.delete_user(actor, admin)


@pytest.mark.unit
class TestSesiones:
    """Role and password changes must end existing sessions."""

    def test_cambiar_el_rol_invalida_la_sesion(self, admin, gestor):
        from app.models.enums import RoleCode
        from app.models.user import Role

        antes = gestor.session_epoch
        gestor.add_role(Role.get(RoleCode.ADMINISTRADOR))
        db.session.commit()

        assert gestor.session_epoch > antes, (
            'Un cambio de rol debe invalidar las sesiones abiertas.'
        )

    def test_el_identificador_de_sesion_incluye_la_epoca(self, gestor):
        assert gestor.get_id() == f'{gestor.id}:{gestor.session_epoch}'

    def test_desactivar_una_cuenta_invalida_la_sesion(self, admin, gestor):
        from app.models.enums import UserStatus
        from app.services import user_service

        antes = gestor.session_epoch
        user_service.update_user(admin, gestor, estado=UserStatus.INACTIVO)

        assert gestor.session_epoch > antes
