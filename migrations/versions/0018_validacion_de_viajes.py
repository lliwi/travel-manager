"""un validador aprueba el viaje antes de que se compre

El rol «validador» y, en el viaje, a quién se le pidió, qué contestó y cuándo.
Solo la última petición vive en la fila; todas las peticiones y respuestas
están en la auditoría.

El estado de la validación admite NULL a propósito: un viaje que nadie ha
enviado a validar no tiene respuesta, y «sin pedir» no es una cuarta
respuesta.

Revision ID: 0018_validacion_de_viajes
Revises: 0017_cuentas_contables
Create Date: 2026-11-15 16:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

import app.models.base

revision = '0018_validacion_de_viajes'
down_revision = '0017_cuentas_contables'
branch_labels = None
depends_on = None

_ROLES_ANTES = ('administrador', 'gestor', 'usuario')
_ROLES = _ROLES_ANTES + ('validador',)
_ESTADOS = ('pendiente', 'aprobada', 'rechazada')


def _en(columna, valores):
    return f"{columna} IN ('" + "', '".join(valores) + "')"


def upgrade():
    op.drop_constraint(op.f('ck_roles_codigo_valido'), 'roles', type_='check')
    op.create_check_constraint(
        op.f('ck_roles_codigo_valido'), 'roles', _en('codigo', _ROLES),
    )

    op.add_column('trips', sa.Column('validador_id', app.models.base.GUID(), nullable=True))
    op.add_column('trips', sa.Column('validacion_estado', sa.String(length=40), nullable=True))
    op.add_column('trips', sa.Column(
        'validacion_solicitada_en', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trips', sa.Column(
        'validacion_solicitada_por_id', app.models.base.GUID(), nullable=True))
    op.add_column('trips', sa.Column(
        'validacion_resuelta_en', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trips', sa.Column('validacion_comentario', sa.Text(), nullable=True))
    op.add_column('trips', sa.Column('validacion_version', sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f('fk_trips_validador_id_users'), 'trips', 'users', ['validador_id'], ['id'])
    op.create_foreign_key(
        op.f('fk_trips_validacion_solicitada_por_id_users'), 'trips', 'users',
        ['validacion_solicitada_por_id'], ['id'])
    op.create_index(op.f('ix_trips_validador_id'), 'trips', ['validador_id'])
    op.create_index(op.f('ix_trips_validacion_estado'), 'trips', ['validacion_estado'])
    op.create_check_constraint(
        op.f('ck_trips_validacion_estado_valido'), 'trips',
        _en('validacion_estado', _ESTADOS),
    )


def downgrade():
    op.drop_constraint(op.f('ck_trips_validacion_estado_valido'), 'trips', type_='check')
    op.drop_index(op.f('ix_trips_validacion_estado'), table_name='trips')
    op.drop_index(op.f('ix_trips_validador_id'), table_name='trips')
    op.drop_constraint(
        op.f('fk_trips_validacion_solicitada_por_id_users'), 'trips', type_='foreignkey')
    op.drop_constraint(op.f('fk_trips_validador_id_users'), 'trips', type_='foreignkey')
    for columna in ('validacion_version', 'validacion_comentario',
                    'validacion_resuelta_en', 'validacion_solicitada_por_id',
                    'validacion_solicitada_en', 'validacion_estado', 'validador_id'):
        op.drop_column('trips', columna)

    op.execute(
        "DELETE FROM user_roles WHERE role_id IN "
        "(SELECT id FROM roles WHERE codigo = 'validador')"
    )
    op.execute("DELETE FROM roles WHERE codigo = 'validador'")
    op.drop_constraint(op.f('ck_roles_codigo_valido'), 'roles', type_='check')
    op.create_check_constraint(
        op.f('ck_roles_codigo_valido'), 'roles', _en('codigo', _ROLES_ANTES),
    )
