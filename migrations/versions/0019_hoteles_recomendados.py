"""los hoteles que la organización recomienda en cada ciudad

Los mantiene el gestor y son lo que el planificador ofrece como alojamiento
en una ciudad que tenga alguno, en lugar de lo que devuelva el buscador: un
hotel con tarifa acordada es una decisión de la organización, y uno de Google
Hotels es solo lo que había ese día.

Revision ID: 0019_hoteles_recomendados
Revises: 0018_validacion_de_viajes
Create Date: 2026-11-15 18:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

import app.models.base

revision = '0019_hoteles_recomendados'
down_revision = '0018_validacion_de_viajes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'hoteles_recomendados',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_by_id', app.models.base.GUID(), nullable=True),
        sa.Column('nombre', sa.String(length=250), nullable=False),
        sa.Column('ciudad', sa.String(length=160), nullable=False),
        sa.Column('ciudad_clave', sa.String(length=160), nullable=False),
        sa.Column('pais', sa.String(length=2), nullable=True),
        sa.Column('zona', sa.String(length=160), nullable=True),
        sa.Column('direccion', sa.String(length=300), nullable=True),
        sa.Column('precio_noche', sa.Numeric(10, 2), nullable=True),
        sa.Column('moneda', sa.String(length=3), nullable=True),
        sa.Column('enlace', sa.String(length=500), nullable=True),
        sa.Column('notas', sa.Text(), nullable=True),
        sa.Column('activo', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('actualizado_por_id', app.models.base.GUID(), nullable=True),
        sa.ForeignKeyConstraint(['deleted_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['actualizado_por_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_hoteles_recomendados_ciudad_clave', 'hoteles_recomendados',
                    ['ciudad_clave'])
    op.create_index('ix_hoteles_recomendados_is_deleted', 'hoteles_recomendados',
                    ['is_deleted'])


def downgrade():
    op.drop_index('ix_hoteles_recomendados_is_deleted', table_name='hoteles_recomendados')
    op.drop_index('ix_hoteles_recomendados_ciudad_clave', table_name='hoteles_recomendados')
    op.drop_table('hoteles_recomendados')
