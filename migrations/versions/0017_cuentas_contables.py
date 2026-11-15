"""a qué cuenta contable va cada tipo de gasto

Una fila por tipo de gasto que tenga cuenta. Los que no la tienen no tienen
fila, y los informes los muestran como «sin cuenta asignada» en vez de
llevarlos a una cuenta que nadie eligió.

De paso, «dietas» entra como tipo de servicio: es de lo primero que pide
contabilidad y no había dónde anotarlas, así que acababan como «otro» y no se
podían separar del resto.

Revision ID: 0017_cuentas_contables
Revises: 0016_viaje_facturable
Create Date: 2026-11-15 14:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

import app.models.base

revision = '0017_cuentas_contables'
down_revision = '0016_viaje_facturable'
branch_labels = None
depends_on = None

_GASTOS = (
    'vuelo', 'tren', 'autobus', 'ferry', 'traslado', 'otro_transporte',
    'alojamiento', 'vehiculo', 'dietas', 'restaurante', 'seguro', 'visado',
    'parking', 'entrada', 'conectividad', 'otro',
)
_SERVICIOS_ANTES = (
    'seguro', 'visado', 'parking', 'restaurante', 'entrada', 'conectividad', 'otro',
)
_SERVICIOS = _SERVICIOS_ANTES[:-1] + ('dietas', 'otro')


def _en(columna, valores):
    return f"{columna} IN ('" + "', '".join(valores) + "')"


def upgrade():
    op.create_table(
        'cuentas_contables',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tipo_gasto', sa.String(length=40), nullable=False),
        sa.Column('cuenta', sa.String(length=40), nullable=False),
        sa.Column('descripcion', sa.String(length=200), nullable=True),
        sa.Column('actualizada_por_id', app.models.base.GUID(), nullable=True),
        sa.ForeignKeyConstraint(['actualizada_por_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tipo_gasto', name='uq_cuentas_contables_tipo_gasto'),
        sa.CheckConstraint(_en('tipo_gasto', _GASTOS),
                           name='ck_cuentas_contables_tipo_gasto_valido'),
    )

    op.drop_constraint(op.f('ck_other_services_tipo_valido'), 'other_services',
                       type_='check')
    op.create_check_constraint(
        op.f('ck_other_services_tipo_valido'), 'other_services', _en('tipo', _SERVICIOS),
    )


def downgrade():
    op.execute("UPDATE other_services SET tipo = 'otro' WHERE tipo = 'dietas'")
    op.drop_constraint(op.f('ck_other_services_tipo_valido'), 'other_services',
                       type_='check')
    op.create_check_constraint(
        op.f('ck_other_services_tipo_valido'), 'other_services',
        _en('tipo', _SERVICIOS_ANTES),
    )
    op.drop_table('cuentas_contables')
