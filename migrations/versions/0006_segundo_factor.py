"""segundo factor de autenticación (TOTP)

Tres columnas en «users» y una tabla para los códigos de recuperación.

El secreto se guarda cifrado porque es equivalente a una contraseña: quien lo
lea puede generar códigos válidos para siempre. Los códigos de recuperación se
guardan con hash por el mismo motivo, y se marcan como usados en lugar de
borrarse, de modo que «alguien usó un código de recuperación» siga teniendo
respuesta después -- que es la señal de que se perdió un teléfono, o de que
alguien más tiene los códigos.

«mfa_ultimo_paso» es lo que impide reutilizar un código. TOTP acepta el mismo
número durante toda una ventana de treinta segundos, así que sin recordar a qué
paso pertenecía el último aceptado, un código leído por encima del hombro vale
una segunda vez.

Revision ID: 0006_segundo_factor
Revises: 0005_planificacion
Create Date: 2026-09-20 20:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

from app.models.base import GUID

revision = '0006_segundo_factor'
down_revision = '0005_planificacion'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('mfa_secret_cifrado', sa.Text(), nullable=True))
        batch.add_column(
            sa.Column('mfa_activado_en', sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column('mfa_ultimo_paso', sa.BigInteger(), nullable=True))

    op.create_table(
        'mfa_recovery_codes',
        sa.Column('id', GUID(), primary_key=True),
        sa.Column('user_id', GUID(), nullable=False),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('usado_en', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE',
            name='fk_mfa_recovery_codes_user_id_users',
        ),
    )
    op.create_index(
        'ix_mfa_recovery_codes_user_id', 'mfa_recovery_codes', ['user_id'],
    )


def downgrade():
    op.drop_index('ix_mfa_recovery_codes_user_id', table_name='mfa_recovery_codes')
    op.drop_table('mfa_recovery_codes')

    with op.batch_alter_table('users') as batch:
        batch.drop_column('mfa_ultimo_paso')
        batch.drop_column('mfa_activado_en')
        batch.drop_column('mfa_secret_cifrado')
