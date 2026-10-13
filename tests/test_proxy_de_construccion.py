"""El proxy para construir las imágenes, en setup.sh y start.sh.

Lo que falló en producción, detrás de un proxy corporativo: setup.sh no leía
BUILD_PROXY, start.sh lo buscaba en un .env que nadie crea, y aunque lo
hubieran leído, quien descarga las imágenes base es el demonio de Docker, que
no usa los --build-arg. El error -- «no such host» resolviendo auth.docker.io
-- apuntaba al DNS y no a nada de esto.

Se ejecuta el bash de verdad, con un «docker» falso delante en el PATH.
"""
import os
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
LIB = RAIZ / 'scripts' / 'lib' / 'proxy.sh'

pytestmark = pytest.mark.unit


@pytest.fixture
def instalacion(tmp_path):
    """A fake checkout: docker/.env, and a docker binary that answers info."""
    (tmp_path / 'docker').mkdir()
    (tmp_path / 'docker' / '.env').write_text('SECRET_KEY=x\n')
    binarios = tmp_path / 'bin'
    binarios.mkdir()
    docker = binarios / 'docker'
    docker.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *HTTPSProxy*) printf "%s" "$DEMONIO_HTTPS" ;;\n'
        '  *HTTPProxy*) printf "%s" "$DEMONIO_HTTP" ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    return tmp_path


def _bash(instalacion, script, **entorno):
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'no_proxy')}
    env.update({'PATH': f'{instalacion / "bin"}:{env["PATH"]}',
                'DEMONIO_HTTPS': '', 'DEMONIO_HTTP': ''})
    env.update(entorno)
    prologo = (
        f'ROOT="{instalacion}"; ENV_FILE="{instalacion}/docker/.env"\n'
        'info() { echo "INFO $*"; }; warn() { echo "WARN $*"; }\n'
        'fail() { echo "FAIL $*"; }\n'
        'compose() { echo "COMPOSE $*"; }\n'
        f'source "{LIB}"\n'
    )
    return subprocess.run(['bash', '-c', prologo + script], env=env,
                          capture_output=True, text=True, timeout=30)


class TestDondeSeBuscaElProxy:
    def test_en_docker_env_que_es_donde_lo_pone_la_plantilla(self, instalacion):
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'proxy_de_construccion')

        assert r.stdout == 'http://proxy.corp:3128'

    def test_y_todavia_en_el_env_de_la_raiz(self, instalacion):
        """Where start.sh used to look: installations that put it there."""
        (instalacion / '.env').write_text('BUILD_PROXY="http://viejo:8080"\n')

        assert _bash(instalacion, 'proxy_de_construccion').stdout == 'http://viejo:8080'

    def test_si_no_esta_escrito_vale_el_del_entorno(self, instalacion):
        r = _bash(instalacion, 'proxy_de_construccion', HTTPS_PROXY='http://env:3128')

        assert r.stdout == 'http://env:3128'

    def test_la_opcion_de_start_manda_sobre_lo_configurado(self, instalacion):
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'PROXY_FORZADO=http://otro:1 proxy_de_construccion')

        assert r.stdout == 'http://otro:1'

    def test_sin_nada_no_hay_proxy(self, instalacion):
        assert _bash(instalacion, 'proxy_de_construccion').stdout == ''


class TestLaContrasenaDelProxyNoSeEnsena:
    def test_se_quita_al_mostrarlo(self, instalacion):
        """The old «${proxy%%@*}» printed exactly the part with the password."""
        r = _bash(instalacion, 'sin_credenciales "http://usuario:clave@proxy.corp:3128"')

        assert r.stdout == 'http://proxy.corp:3128'

    def test_ni_al_construir(self, instalacion):
        (instalacion / 'docker' / '.env').write_text(
            'BUILD_PROXY=http://usuario:clave@proxy.corp:3128\n')

        r = _bash(instalacion, 'construir_imagenes',
                  DEMONIO_HTTPS='http://usuario:clave@proxy.corp:3128')

        mensajes = [linea for linea in r.stdout.splitlines() if not linea.startswith('COMPOSE')]
        assert not [m for m in mensajes if 'clave' in m], mensajes


class TestElDemonioDeDockerTambienLoNecesita:
    def test_sin_proxy_en_el_demonio_se_para_y_dice_como_ponerlo(self, instalacion):
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'construir_imagenes; echo "rc=$?"')

        assert 'rc=1' in r.stdout
        assert 'COMPOSE' not in r.stdout, 'No debe intentar construir.'
        assert 'docker.service.d/http-proxy.conf' in r.stderr
        assert 'systemctl restart docker' in r.stderr

    def test_avisa_de_que_reiniciar_docker_para_todo(self, instalacion):
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'construir_imagenes')

        assert 'detiene todos los contenedores' in r.stderr

    def test_se_lee_el_proxy_http_si_el_https_no_esta(self, instalacion):
        """«docker info» answers an empty string, successfully, for a missing field."""
        r = _bash(instalacion, 'proxy_del_demonio', DEMONIO_HTTP='http://proxy.corp:3128')

        assert r.stdout == 'http://proxy.corp:3128'

    def test_con_el_demonio_configurado_construye(self, instalacion):
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'construir_imagenes; echo "rc=$?"',
                  DEMONIO_HTTPS='http://proxy.corp:3128')

        assert 'rc=0' in r.stdout
        assert 'COMPOSE build' in r.stdout

    def test_sin_proxy_que_hacer_no_se_mira_el_demonio(self, instalacion):
        r = _bash(instalacion, 'construir_imagenes; echo "rc=$?"')

        assert 'rc=0' in r.stdout
        assert 'COMPOSE build' in r.stdout
        assert '--build-arg' not in r.stdout


class TestLaConstruccionRecibeElProxy:
    def test_en_mayusculas_y_en_minusculas(self, instalacion):
        """pip and apt read the lowercase form; Docker only predefines the case passed."""
        (instalacion / 'docker' / '.env').write_text('BUILD_PROXY=http://proxy.corp:3128\n')

        r = _bash(instalacion, 'construir_imagenes', DEMONIO_HTTPS='http://proxy.corp:3128')

        orden = next(linea for linea in r.stdout.splitlines() if linea.startswith('COMPOSE'))
        for nombre in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                       'NO_PROXY', 'no_proxy'):
            assert f'--build-arg {nombre}=' in orden, nombre


class TestLosDosScriptsUsanLoMismo:
    @pytest.mark.parametrize('script', ['setup.sh', 'start.sh'])
    def test_cargan_la_libreria(self, script):
        assert 'scripts/lib/proxy.sh' in (RAIZ / script).read_text()

    def test_setup_no_construye_por_su_cuenta(self):
        """A bare «${COMPOSE} build» is what ignored BUILD_PROXY."""
        texto = (RAIZ / 'setup.sh').read_text()

        assert '${COMPOSE} build' not in texto
        assert 'construir_imagenes' in texto
