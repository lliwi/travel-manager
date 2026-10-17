"""Repetir setup.sh sobre una instalación que ya existe.

Es seguro mientras docker/.env siga ahí. El caso que no: datos de una
instalación anterior y un .env ausente o con un secreto vacío -- el código
copiado de nuevo sin traer el .env --. Generar secretos nuevos encima de esos
datos deja ilegibles para siempre las claves cifradas y hace que PostgreSQL
rechace la contraseña. Se ejecuta el setup.sh de verdad, con un «docker»
falso que dice si existe el volumen de datos.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.unit


@pytest.fixture
def proyecto(tmp_path):
    for relativo in ('setup.sh', '.env.example', 'scripts/lib/proxy.sh',
                     'scripts/lib/tls.sh', 'docker/docker-compose.yml'):
        destino = tmp_path / relativo
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(RAIZ / relativo, destino)

    binarios = tmp_path / 'bin'
    binarios.mkdir()
    docker = binarios / 'docker'
    docker.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  --version) echo "Docker version 29.9.0" ;;\n'
        '  "volume inspect"*) exit "$HAY_DATOS" ;;\n'
        '  *"config --format json"*) echo \'{"name": "travel-manager"}\' ;;\n'
        '  *build*) echo "CONSTRUYENDO" ; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    docker.chmod(0o755)
    return tmp_path


def _setup(proyecto, hay_datos):
    entorno = dict(os.environ, PATH=f'{proyecto / "bin"}:{os.environ["PATH"]}',
                   HAY_DATOS='0' if hay_datos else '1')
    return subprocess.run(['bash', str(proyecto / 'setup.sh')], env=entorno,
                          capture_output=True, text=True, timeout=120)


def _env_valido(proyecto):
    plantilla = (proyecto / '.env.example').read_text()
    texto = plantilla.replace('POSTGRES_PASSWORD=', 'POSTGRES_PASSWORD=LaDeVerdad123', 1)
    lineas = [
        'SECRETS_ENCRYPTION_KEY=clave-de-verdad'
        if linea.startswith('SECRETS_ENCRYPTION_KEY=') else linea
        for linea in texto.splitlines()
    ]
    (proyecto / 'docker' / '.env').write_text('\n'.join(lineas) + '\n')


class TestConDatosAnteriores:
    def test_sin_env_se_niega_y_no_escribe_nada(self, proyecto):
        r = _setup(proyecto, hay_datos=True)

        assert r.returncode == 1
        assert 'falta docker/.env' in r.stdout + r.stderr
        assert not (proyecto / 'docker' / '.env').exists(), 'No debe crear un .env nuevo.'
        assert 'CONSTRUYENDO' not in r.stdout

    def test_con_un_secreto_vacio_tambien_se_niega(self, proyecto):
        _env_valido(proyecto)
        env = proyecto / 'docker' / '.env'
        env.write_text(env.read_text().replace('POSTGRES_PASSWORD=LaDeVerdad123',
                                               'POSTGRES_PASSWORD='))
        antes = env.read_text()

        r = _setup(proyecto, hay_datos=True)

        assert r.returncode == 1
        assert env.read_text() == antes, 'No debe tocar el .env.'

    def test_dice_como_recuperarlo_y_como_empezar_de_cero(self, proyecto):
        r = _setup(proyecto, hay_datos=True)

        assert 'docker/.env.<fecha>.bak' in r.stderr
        assert 'down -v' in r.stderr

    def test_con_el_env_completo_sigue_y_conserva_los_secretos(self, proyecto):
        _env_valido(proyecto)

        r = _setup(proyecto, hay_datos=True)

        env = (proyecto / 'docker' / '.env').read_text()
        assert 'CONSTRUYENDO' in r.stdout, 'Debe pasar la comprobación y llegar a construir.'
        assert 'POSTGRES_PASSWORD=LaDeVerdad123' in env
        assert 'SECRETS_ENCRYPTION_KEY=clave-de-verdad' in env

    def test_deja_copia_del_env_antes_de_tocarlo(self, proyecto):
        _env_valido(proyecto)
        original = (proyecto / 'docker' / '.env').read_text()

        _setup(proyecto, hay_datos=True)

        copias = list((proyecto / 'docker').glob('.env.*.bak'))
        assert len(copias) == 1
        assert copias[0].read_text() == original
        assert oct(copias[0].stat().st_mode)[-3:] == '600'


class TestSinDatosAnteriores:
    def test_una_instalacion_nueva_genera_sus_secretos(self, proyecto):
        r = _setup(proyecto, hay_datos=False)

        env = (proyecto / 'docker' / '.env').read_text()
        assert 'CONSTRUYENDO' in r.stdout
        assert 'POSTGRES_PASSWORD=cambie' not in env
        assert 'SECRETS_ENCRYPTION_KEY=cambie' not in env
