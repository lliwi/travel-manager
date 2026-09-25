"""copias de seguridad bajo demanda desde el panel

Una copia completa -- base de datos, ajustes, documentos y la clave con la que
están cifrados los secretos -- se genera como un ZIP cifrado que se descarga,
y el mismo ZIP se importa para restaurar. Esta tabla sigue cada una de esas
operaciones, y es lo único que ni se copia ni se sustituye: una restauración
que borrara su propio seguimiento terminaría sin decir cómo le fue.

Revision ID: 0012_copias_de_seguridad
Revises: 0011_parametros_y_autoajuste_ia
Create Date: 2026-09-25 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

import app.models.base


revision = '0012_copias_de_seguridad'
down_revision = '0011_parametros_y_autoajuste_ia'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'backup_jobs',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tipo', sa.String(length=40), nullable=False),
        sa.Column('estado', sa.String(length=40), nullable=False),
        sa.Column('objeto', sa.String(length=300), nullable=True),
        sa.Column('nombre_fichero', sa.String(length=200), nullable=True),
        sa.Column('tamano', sa.BigInteger(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('resumen', app.models.base.JSONBType(), nullable=True),
        sa.Column('progreso', sa.String(length=200), nullable=True),
        sa.Column('error', sa.String(length=500), nullable=True),
        sa.Column('iniciado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('terminado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('descargado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lanzado_por_id', app.models.base.GUID(), nullable=True),
        sa.Column('lanzado_por_nombre', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['lanzado_por_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("tipo IN ('copia', 'restauracion')",
                           name='ck_backup_jobs_tipo_valido'),
        sa.CheckConstraint("estado IN ('pendiente', 'en_curso', 'completado', 'error')",
                           name='ck_backup_jobs_estado_valido'),
    )
    op.create_index('ix_backup_jobs_created_at', 'backup_jobs', ['created_at'])
    op.create_index('ix_backup_jobs_tipo', 'backup_jobs', ['tipo'])
    op.create_index('ix_backup_jobs_estado', 'backup_jobs', ['estado'])


def downgrade():
    op.drop_table('backup_jobs')
