"""Inline scripts and the policy that decides whether they run.

This is the failure mode the suite exists to catch: the page is served whole,
looks right, and does nothing. Nothing fails on the server, the template is
correct, and the only trace is a line in the browser console that nobody reads
until somebody reports that a button «no hace nada». It happened: the model
picker in Administración → Proveedores de IA was refused by the policy, so the
list of models the endpoint published never reached the field.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
PLANTILLAS = RAIZ / 'app' / 'templates'

#: `<script>` without a `src`, i.e. one whose body the policy has to allow.
EN_LINEA = re.compile(r'<script(?![^>]*\bsrc=)([^>]*)>')


def _scripts_en_linea():
    for ruta in sorted(PLANTILLAS.rglob('*.html')):
        for atributos in EN_LINEA.findall(ruta.read_text()):
            yield ruta.relative_to(RAIZ), atributos


@pytest.mark.security
class TestLaCSPDejaCorrerLoQueLaPlantillaEscribio:
    def test_todo_script_en_linea_lleva_nonce(self):
        sin_nonce = [
            str(ruta) for ruta, atributos in _scripts_en_linea()
            if 'csp_nonce()' not in atributos
        ]

        assert not sin_nonce, (
            'Estos <script> en línea no llevan nonce y el navegador los '
            'rechazará en producción sin que falle nada en el servidor: '
            + ', '.join(sin_nonce)
        )

    def test_el_nonce_llega_a_la_politica(self):
        """A nonce in the template means nothing if the header omits it."""
        fuente = (RAIZ / 'app' / '__init__.py').read_text()

        assert "content_security_policy_nonce_in=['script-src']" in fuente

    def test_no_se_abre_unsafe_inline(self):
        """The nonce exists so the escape hatch stays shut."""
        fuente = (RAIZ / 'app' / '__init__.py').read_text()
        script_src = re.search(r"'script-src':\s*(\[[^\]]*\])", fuente)

        assert script_src, 'La CSP ya no declara script-src'
        assert "'unsafe-inline'" not in script_src.group(1)

    def test_hay_scripts_en_linea_que_comprobar(self):
        """Guard against the checks above passing because they found nothing."""
        assert list(_scripts_en_linea())
