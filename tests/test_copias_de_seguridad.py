"""The backup scripts, pinned by what they must not stop doing.

A shell script cannot be exercised from this suite without a live stack, so
what is checked here is the shape of the thing: that both stores are covered,
that a half-written backup is not left looking whole, and that a restore ends
by checking the audit chain. Those are the properties whose absence is only
discovered on the day somebody needs them.
"""
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
BACKUP = RAIZ / 'scripts' / 'backup.sh'
RESTORE = RAIZ / 'scripts' / 'restore.sh'


@pytest.mark.unit
class TestLosScriptsExisten:
    def test_estan_y_son_ejecutables(self):
        for script in (BACKUP, RESTORE):
            assert script.exists(), f'Falta {script.name}'
            assert script.stat().st_mode & 0o111, f'{script.name} no es ejecutable'

    def test_paran_al_primer_error(self):
        """Without this a failed step scrolls past and the script says «listo»."""
        for script in (BACKUP, RESTORE):
            assert 'set -Eeuo pipefail' in script.read_text()


@pytest.mark.unit
class TestLaCopiaCubreLosDosAlmacenes:
    """The database holds the object keys and the store holds the objects.

    Backing up one alone, or the two at different moments, restores an
    itinerary where every document points at a file that is not there.
    """

    def test_incluye_la_base_de_datos(self):
        assert 'pg_dump' in BACKUP.read_text()

    def test_incluye_los_documentos(self):
        assert 'volcar_documentos.py' in BACKUP.read_text()

    def test_una_copia_a_medias_no_se_deja(self):
        texto = BACKUP.read_text()

        assert 'trap limpiar_si_falla EXIT' in texto
        assert 'rm -rf "${TRABAJO}"' in texto

    def test_lleva_sumas_de_verificacion(self):
        """So a restore can tell a truncated copy from a whole one."""
        assert 'sha256sum' in BACKUP.read_text()

    def test_lleva_manifiesto(self):
        assert 'manifiesto.txt' in BACKUP.read_text()


@pytest.mark.unit
class TestLaRestauracionSeComprueba:
    def test_verifica_las_sumas_antes_de_tocar_nada(self):
        texto = RESTORE.read_text()
        antes = texto.index('sha256sum --quiet -c sumas.sha256')
        restaura = texto.index('pg_restore')

        assert antes < restaura, (
            'Comprobar la copia después de haberla aplicado no sirve de nada.'
        )

    def test_termina_comprobando_la_cadena_de_auditoria(self):
        """If the chain does not add up, the copy was incomplete or altered."""
        assert 'flask verify-audit' in RESTORE.read_text()

    def test_detiene_la_aplicacion_mientras_dura(self):
        assert 'stop web worker beat' in RESTORE.read_text()

    def test_vuelve_a_arrancar_aunque_falle(self):
        """A restore that dies half way must not leave the stack down."""
        assert 'trap arrancar EXIT' in RESTORE.read_text()

    def test_pide_confirmacion(self):
        texto = RESTORE.read_text()

        assert 'ASUMIR_SI' in texto
        assert 'Se sobrescribirán los datos actuales' in texto


@pytest.mark.unit
class TestElVolcadoUsaElClienteDeLaAplicacion:
    """No extra image, no credentials on a command line.

    The first version shelled out to «minio/mc», which is one more thing to
    have available and one more place for the keys to appear.
    """

    def test_volcado_y_restauracion_comparten_cliente(self):
        for nombre in ('volcar_documentos.py', 'restaurar_documentos.py'):
            texto = (RAIZ / 'scripts' / nombre).read_text()
            assert 'storage_service' in texto
            assert 'minio/mc' not in texto

    def test_el_tar_no_se_escribe_en_la_salida_estandar(self):
        """Creating the app logs a line, and a log line in front of a tar makes
        it not a tar -- corruption nobody notices until they restore."""
        texto = (RAIZ / 'scripts' / 'volcar_documentos.py').read_text()

        assert 'sys.stdout.buffer' not in texto
        assert 'tarfile.open(destino' in texto
