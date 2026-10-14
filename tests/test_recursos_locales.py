"""Nada de la interfaz se descarga de fuera.

Visto en producción: detrás de un proxy corporativo, Bootstrap y sus iconos
desde cdn.jsdelivr.net y la fuente desde Google Fonts llegaban bloqueados, y
la aplicación se mostraba sin estilos. Todo se sirve ahora desde
static/vendor, y esto falla si alguien vuelve a enlazar algo de fuera.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent / 'app'
ESTATICOS = RAIZ / 'static'

pytestmark = pytest.mark.unit

#: An external URL in an attribute or CSS that the browser would fetch.
EXTERNO = re.compile(
    r'''(?:\b(?:src|href)\s*=\s*["']|@import\s+(?:url\()?["']?|url\(\s*["']?)'''
    r'''(?:https?:)?//''',
    re.I,
)


def _ficheros():
    for patron in ('templates/**/*.html', 'static/css/**/*.css', 'static/js/**/*.js',
                   'static/vendor/**/*.css'):
        yield from RAIZ.glob(patron)


class TestNadaDeFuera:
    def test_ninguna_plantilla_ni_hoja_pide_nada_externo(self):
        externos = []
        for ruta in _ficheros():
            for numero, linea in enumerate(ruta.read_text().splitlines(), 1):
                if EXTERNO.search(linea):
                    externos.append(f'{ruta.relative_to(RAIZ)}:{numero}')

        assert not externos, 'Recursos externos: ' + ', '.join(externos)

    def test_la_politica_de_contenido_no_admite_un_cdn(self):
        """Allowing one would only hide the next asset linked from outside."""
        fuente = (RAIZ / '__init__.py').read_text()

        for dominio in ('cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'):
            assert dominio not in fuente, dominio


class TestLoEnlazadoExiste:
    def test_cada_estatico_de_la_plantilla_base(self):
        base = (RAIZ / 'templates' / 'base.html').read_text()
        enlazados = re.findall(r"url_for\('static', filename='([^']+)'\)", base)

        assert any(e.startswith('vendor/') for e in enlazados)
        faltan = [e for e in enlazados if not (ESTATICOS / e).is_file()]
        assert not faltan, faltan

    @pytest.mark.parametrize('hoja', [
        'vendor/inter/inter.css',
        'vendor/bootstrap-icons/bootstrap-icons.min.css',
    ])
    def test_las_fuentes_de_cada_hoja(self, hoja):
        ruta = ESTATICOS / hoja
        fuentes = [u.split('?')[0] for u in re.findall(r'url\(\s*["\']?([^)"\']+)', ruta.read_text())]

        assert fuentes
        faltan = [f for f in fuentes if not (ruta.parent / f).is_file()]
        assert not faltan, faltan

    def test_sin_mapas_que_no_estan(self):
        """A sourceMappingURL points at a file that is not shipped: a 404 per page."""
        for ruta in (ESTATICOS / 'vendor').rglob('*'):
            if ruta.suffix in ('.css', '.js'):
                assert 'sourceMappingURL' not in ruta.read_text(), ruta.name

    def test_hay_icono_de_la_pagina(self):
        base = (RAIZ / 'templates' / 'base.html').read_text()

        assert 'rel="icon"' in base
        assert (ESTATICOS / 'img' / 'favicon.svg').is_file()
