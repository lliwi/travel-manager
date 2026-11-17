"""referencia de viaje VI000001-2026

El formato pasa de VJ-2026-0001 a VI000001-2026: seis cifras que vuelven a
empezar cada año, y el año al final. Los viajes que ya existen se renumeran
conservando su año y su número -- VJ-2026-0009 pasa a ser VI000009-2026 --,
de modo que quien conocía uno reconoce el otro y el contador del año sigue
donde estaba.

La auditoría no se toca: es de solo añadir y cuenta lo que pasó con la
referencia que el viaje tenía entonces. Una búsqueda escrita con el formato
antiguo sigue encontrando el viaje (``trip_service.referencia_actual``).

Revision ID: 0020_referencia_de_viaje
Revises: 0019_hoteles_recomendados
Create Date: 2026-11-17 09:00:00.000000

"""
import re

import sqlalchemy as sa
from alembic import op

revision = '0020_referencia_de_viaje'
down_revision = '0019_hoteles_recomendados'
branch_labels = None
depends_on = None

_ANTIGUA = re.compile(r'^VJ-(\d{4})-(\d+)$')
_NUEVA = re.compile(r'^VI(\d{6,})-(\d{4})$')


def _renombrar(patron, formato):
    conexion = op.get_bind()
    viajes = sa.table('trips', sa.column('id'), sa.column('referencia'))
    filas = conexion.execute(sa.select(viajes.c.id, viajes.c.referencia)).all()
    cambios = []
    for identificador, referencia in filas:
        encontrada = patron.match(referencia or '')
        if encontrada:
            cambios.append((identificador, formato(*encontrada.groups())))

    destinos = [nueva for _, nueva in cambios]
    if len(set(destinos)) != len(destinos):
        raise RuntimeError('La renumeración produciría referencias repetidas.')

    for identificador, nueva in cambios:
        conexion.execute(
            viajes.update().where(viajes.c.id == identificador).values(referencia=nueva)
        )


def upgrade():
    _renombrar(_ANTIGUA, lambda anio, numero: f'VI{int(numero):06d}-{anio}')


def downgrade():
    _renombrar(_NUEVA, lambda numero, anio: f'VJ-{anio}-{int(numero):04d}')
