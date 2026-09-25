"""parámetros avanzados de los modelos y autoajuste

Hasta ahora un proveedor tenía temperatura y máximo de tokens, y nada más.
Cada modelo, sin embargo, quiere lo suyo: uno pequeño necesita que se le
penalice repetir, Ollama trunca en silencio lo que no cabe en su ventana, y un
ajuste bueno para extraer datos es malo para redactar. De ahí tres cosas:

* ``ai_provider_configs.parametros_avanzados``: los de todo el endpoint.
* ``ai_parameter_profiles``: los de un modelo, para todas las tareas o para
  una. Es la capa que escribe el autoajuste.
* ``ai_runs.parametros``: lo que de verdad se envió en cada ejecución, tras
  todas las capas. Sin eso no hay forma de comparar dos respuestas.
* ``ai_autotune_runs``: cada búsqueda, con todos sus ensayos y no solo el
  ganador, para que «¿por qué eligió esto?» se pueda contestar leyendo la fila.

La unicidad del perfil lleva NULLS NOT DISTINCT: sin ella PostgreSQL admitiría
cualquier número de perfiles «para todas las tareas» del mismo modelo.

Revision ID: 0011_parametros_y_autoajuste_ia
Revises: 0010_procedencia_buscador
Create Date: 2026-09-25 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

import app.models.base


revision = '0011_parametros_y_autoajuste_ia'
down_revision = '0010_procedencia_buscador'
branch_labels = None
depends_on = None

_TAREAS = ('extract_document', 'answer_trip_question', 'summarize_trip',
           'analyze_risks', 'research_public_info', 'classify_document',
           'explain_alert', 'plan_trip')
_ORIGENES = ('manual', 'autoajuste')
_ESTADOS = ('pendiente', 'en_curso', 'completado', 'error', 'cancelado')


def _en(columna, valores):
    return f"{columna} IN ('" + "', '".join(valores) + "')"


def upgrade():
    op.add_column(
        'ai_runs',
        sa.Column('parametros', app.models.base.JSONBType(), nullable=True),
    )
    op.add_column(
        'ai_provider_configs',
        sa.Column('parametros_avanzados', app.models.base.JSONBType(), nullable=True),
    )

    op.create_table(
        'ai_autotune_runs',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tarea', sa.String(length=40), nullable=False),
        sa.Column('provider_config_id', app.models.base.GUID(), nullable=False),
        sa.Column('modelo', sa.String(length=160), nullable=False),
        sa.Column('estado', sa.String(length=40), nullable=False),
        sa.Column('espacio', app.models.base.JSONBType(), nullable=False),
        sa.Column('presupuesto', sa.Integer(), nullable=False),
        sa.Column('repeticiones', sa.Integer(), nullable=False),
        sa.Column('aplicar_si_mejora', sa.Boolean(), nullable=False),
        sa.Column('automatico', sa.Boolean(), nullable=False),
        sa.Column('casos', sa.Integer(), nullable=True),
        sa.Column('ensayos', app.models.base.JSONBType(), nullable=False),
        sa.Column('parametros_base', app.models.base.JSONBType(), nullable=True),
        sa.Column('parametros_mejores', app.models.base.JSONBType(), nullable=True),
        sa.Column('calidad_base', sa.Numeric(5, 1), nullable=True),
        sa.Column('calidad_mejor', sa.Numeric(5, 1), nullable=True),
        sa.Column('error', sa.String(length=500), nullable=True),
        sa.Column('celery_task_id', sa.String(length=64), nullable=True),
        sa.Column('iniciado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('terminado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('aplicado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('aplicado_por_id', app.models.base.GUID(), nullable=True),
        sa.Column('parametros_previos', app.models.base.JSONBType(), nullable=True),
        sa.Column('revertido_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lanzado_por_id', app.models.base.GUID(), nullable=True),
        sa.ForeignKeyConstraint(['provider_config_id'], ['ai_provider_configs.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['aplicado_por_id'], ['users.id']),
        sa.ForeignKeyConstraint(['lanzado_por_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(_en('tarea', _TAREAS), name='ck_ai_autotune_runs_tarea_valido'),
        sa.CheckConstraint(_en('estado', _ESTADOS), name='ck_ai_autotune_runs_estado_valido'),
    )
    op.create_index('ix_ai_autotune_runs_created_at', 'ai_autotune_runs', ['created_at'])
    op.create_index('ix_ai_autotune_runs_estado', 'ai_autotune_runs', ['estado'])
    op.create_index('ix_ai_autotune_runs_provider_config_id', 'ai_autotune_runs',
                    ['provider_config_id'])
    op.create_index('ix_ai_autotune_runs_tarea_created', 'ai_autotune_runs',
                    ['tarea', 'created_at'])

    op.create_table(
        'ai_parameter_profiles',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('provider_config_id', app.models.base.GUID(), nullable=False),
        sa.Column('modelo', sa.String(length=160), nullable=False),
        sa.Column('tarea', sa.String(length=40), nullable=True),
        sa.Column('parametros', app.models.base.JSONBType(), nullable=False),
        sa.Column('origen', sa.String(length=40), nullable=False),
        sa.Column('autotune_run_id', app.models.base.GUID(), nullable=True),
        sa.Column('actualizado_por_id', app.models.base.GUID(), nullable=True),
        sa.ForeignKeyConstraint(['provider_config_id'], ['ai_provider_configs.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['autotune_run_id'], ['ai_autotune_runs.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['actualizado_por_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_config_id', 'modelo', 'tarea',
                            name='uq_ai_parameter_profiles_alcance',
                            postgresql_nulls_not_distinct=True),
        sa.CheckConstraint(_en('tarea', _TAREAS), name='ck_ai_parameter_profiles_tarea_valido'),
        sa.CheckConstraint(_en('origen', _ORIGENES),
                           name='ck_ai_parameter_profiles_origen_valido'),
    )
    op.create_index('ix_ai_parameter_profiles_created_at', 'ai_parameter_profiles',
                    ['created_at'])
    op.create_index('ix_ai_parameter_profiles_provider_config_id', 'ai_parameter_profiles',
                    ['provider_config_id'])


def downgrade():
    op.drop_table('ai_parameter_profiles')
    op.drop_table('ai_autotune_runs')
    op.drop_column('ai_provider_configs', 'parametros_avanzados')
    op.drop_column('ai_runs', 'parametros')
