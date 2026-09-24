"""un dato que viene del buscador no es ni un documento ni una persona

Al poder crear un viaje desde los vuelos y alojamientos que devuelve el
conector, aparece una cuarta procedencia. Las tres de antes no le sirven:
«documento» exige un fragmento literal en un texto que aquí no existe, «ia»
dice que lo infirió un modelo, y «manual» diría que alguien tecleó ese horario,
cuando lo único que hizo fue elegir una fila.

Importa porque de eso depende cómo se lee el dato: los precios de Google son
orientativos y nada de esto es una reserva, así que la fila tiene que poder
decir de dónde salió y quedar marcada para que alguien la confirme.

El CHECK sobre la lista se reescribe entero en lugar de ampliarse, como en
0007: así queda escrito qué valores existían cuando se aplicó.

Revision ID: 0010_procedencia_buscador
Revises: 0009_proyecto_del_viaje
Create Date: 2026-09-24 12:00:00.000000

"""
from alembic import op

revision = '0010_procedencia_buscador'
down_revision = '0009_proyecto_del_viaje'
branch_labels = None
depends_on = None

_ANTES = ('documento', 'manual', 'ia')
_DESPUES = ('documento', 'manual', 'ia', 'buscador')


def _reescribir(valores):
    """Rewrite field_provenance.origen's CHECK with the given list.

    Raw SQL for the same reason as 0007: ``op.drop_constraint`` runs the name
    through the metadata naming convention, which prepends ``ck_<tabla>_`` to a
    name that already has it. The drop then looks for
    ``ck_field_provenance_ck_field_provenance_origen_valido``, finds nothing,
    and the migration fails on a database that is perfectly fine.
    """
    lista = ', '.join(f"'{v}'" for v in valores)
    op.execute(
        'ALTER TABLE field_provenance '
        'DROP CONSTRAINT ck_field_provenance_origen_valido'
    )
    op.execute(
        'ALTER TABLE field_provenance '
        'ADD CONSTRAINT ck_field_provenance_origen_valido '
        f'CHECK (origen IN ({lista}))'
    )


def upgrade():
    _reescribir(_DESPUES)


def downgrade():
    op.execute("DELETE FROM field_provenance WHERE origen = 'buscador'")
    _reescribir(_ANTES)
