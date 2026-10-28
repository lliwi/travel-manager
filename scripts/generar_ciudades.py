"""Regenerate the city gazetteer the traveller map falls back on.

Reads GeoNames' «cities15000» -- every place of 15 000 inhabitants or more --
and writes the four columns the map needs, so the application never asks a
third party where a city is: it already knows, offline, the way it knows the
outline of the countries it draws.

    python scripts/generar_ciudades.py cities15000.zip

Download the zip from https://download.geonames.org/export/dump/. The data is
GeoNames' under CC BY 4.0; the map credits it.
"""
import csv
import gzip
import io
import sys
import zipfile
from pathlib import Path

SALIDA = Path(__file__).resolve().parent.parent / 'app' / 'data' / 'ciudades.tsv.gz'

#: Above this, a city's other names are kept too: it is the large ones that
#: travel documents name in another language («Monaco di Baviera», «Turin»).
POBLACION_CON_ALIAS = 300_000


def main(ruta):
    with zipfile.ZipFile(ruta) as archivo:
        crudo = archivo.read('cities15000.txt').decode('utf-8')

    filas = []
    for campos in csv.reader(io.StringIO(crudo), delimiter='\t', quoting=csv.QUOTE_NONE):
        nombre, ascii_, alias = campos[1], campos[2], campos[3]
        latitud, longitud, pais = campos[4], campos[5], campos[8]
        poblacion = int(campos[14] or 0)
        nombres = {nombre, ascii_}
        if poblacion >= POBLACION_CON_ALIAS:
            nombres.update(a for a in alias.split(',') if a and len(a) <= 40)
        filas.append((
            '|'.join(sorted(n for n in nombres if n)), pais,
            f'{float(latitud):.2f}', f'{float(longitud):.2f}', str(poblacion),
        ))

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(SALIDA, 'wt', encoding='utf-8', compresslevel=9) as destino:
        destino.write('# GeoNames cities15000, CC BY 4.0 (https://www.geonames.org)\n')
        for fila in sorted(filas, key=lambda f: (f[1], f[0])):
            destino.write('\t'.join(fila) + '\n')
    print(f'{len(filas)} ciudades en {SALIDA} ({SALIDA.stat().st_size // 1024} KB)')


if __name__ == '__main__':
    main(sys.argv[1])
