"""centro de notificaciones

Una notificación es algo que el sistema ya sabe y la persona todavía no: una
alerta que acaba de aparecer en su viaje, una fuente que ha cambiado bajo una
recomendación que leyó la semana pasada, un documento cuyo proceso falló. Sin
un sitio donde ponerlas, la única forma de enterarse es ir a mirar, lo que
significa que se enteran quienes miran y no quienes tienen que saberlo.

La restricción única sobre (usuario, clave) es lo que evita que se acumulen: el
motor de alertas reconcilia y el vigilante de fuentes corre a diario, así que el
mismo hecho vuelve a presentarse una y otra vez. Sin ella, un viaje con una
alerta abierta criaría una notificación cada noche hasta que nadie leyera
ninguna.

Revision ID: 0004_notificaciones
Revises: 0003_varios_servicios
Create Date: 2026-09-20 08:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

import app.models.base


revision = '0004_notificaciones'
down_revision = '0003_varios_servicios'
branch_labels = None
depends_on = None

_TIPOS = ('alerta', 'documento', 'recomendacion', 'viaje')


def upgrade():
    op.create_table(
        'notifications',
        sa.Column('id', app.models.base.GUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('usuario_id', app.models.base.GUID(), nullable=False),
        sa.Column('tipo', sa.String(length=40), nullable=False),
        sa.Column('clave', sa.String(length=200), nullable=False),
        sa.Column('titulo', sa.String(length=300), nullable=False),
        sa.Column('mensaje', sa.Text(), nullable=True),
        sa.Column('enlace', sa.String(length=500), nullable=True),
        sa.Column('trip_id', app.models.base.GUID(), nullable=True),
        sa.Column('datos', app.models.base.JSONBType(), nullable=True),
        sa.Column('leida_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('enviada_por_correo_en', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['usuario_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('usuario_id', 'clave', name='uq_notifications_usuario_clave'),
        sa.CheckConstraint(
            "tipo IN ('" + "', '".join(_TIPOS) + "')",
            name='ck_notifications_tipo',
        ),
    )
    op.create_index('ix_notifications_usuario_id', 'notifications', ['usuario_id'])
    op.create_index('ix_notifications_trip_id', 'notifications', ['trip_id'])
    op.create_index('ix_notifications_tipo', 'notifications', ['tipo'])
    op.create_index(
        'ix_notifications_usuario_leida', 'notifications', ['usuario_id', 'leida_en'],
    )


def downgrade():
    op.drop_index('ix_notifications_usuario_leida', table_name='notifications')
    op.drop_index('ix_notifications_tipo', table_name='notifications')
    op.drop_index('ix_notifications_trip_id', table_name='notifications')
    op.drop_index('ix_notifications_usuario_id', table_name='notifications')
    op.drop_table('notifications')
