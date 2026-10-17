"""Rotar la clave de cifrado sin perder lo guardado.

Cambiar SECRETS_ENCRYPTION_KEY sin más dejaba cada secreto guardado cifrado
con la clave vieja, y decrypt_secret respondía None para todos: nada fallaba,
las cosas simplemente dejaban de funcionar. Se rota con dos claves -- la nueva
y la anterior -- y se re-cifra lo guardado; lo que ninguna abre se informa y
no se toca.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.extensions import db
from app.models.ai import AIProviderConfig
from app.models.enums import AIProviderCode
from app.services import ai_provider_service, rotacion_service, settings_service
from app.utils.crypto import decrypt_secret

RAIZ = Path(__file__).resolve().parent.parent
CLAVE_API = 'sk-la-clave-del-proveedor-1234'
CONTRASENA_LDAP = 'la-del-directorio'


@pytest.fixture
def guardado(app, admin):
    """Secrets stored under the current key, as in a running installation."""
    ai_provider_service.create(admin, 'OpenAI', AIProviderCode.OPENAI, api_key=CLAVE_API)
    settings_service.set_value('LDAP_CONTRASENA', CONTRASENA_LDAP)
    return app.config['SECRETS_ENCRYPTION_KEY']


@pytest.fixture
def clave_nueva(app, guardado):
    """Rotate: the new key in front, the old one as previous."""
    original = app.config['SECRETS_ENCRYPTION_KEY']
    app.config['SECRETS_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    app.config['SECRETS_ENCRYPTION_KEY_PREVIOUS'] = original
    yield app.config['SECRETS_ENCRYPTION_KEY']
    app.config['SECRETS_ENCRYPTION_KEY'] = original
    app.config['SECRETS_ENCRYPTION_KEY_PREVIOUS'] = None


def _legibles_solo_con_la_actual(app):
    app.config['SECRETS_ENCRYPTION_KEY_PREVIOUS'] = None
    config = AIProviderConfig.query.filter_by(nombre='OpenAI').one()
    return (decrypt_secret(config.api_key_encrypted),
            settings_service.get('LDAP_CONTRASENA'))


@pytest.mark.security
class TestRecifrar:
    def test_sin_cambio_de_clave_no_hay_nada_que_hacer(self, guardado):
        estado = rotacion_service.estado()

        assert estado.con_la_actual == 2
        assert not estado.hay_que_recifrar and not estado.ilegibles

    def test_con_la_anterior_se_ve_lo_que_falta(self, clave_nueva):
        estado = rotacion_service.estado()

        assert estado.con_la_anterior == 2
        assert estado.con_la_actual == 0

    def test_tras_recifrar_se_lee_sin_la_clave_anterior(self, app, clave_nueva):
        """Which is what lets the old key be thrown away."""
        estado = rotacion_service.recifrar()

        assert estado.con_la_actual == 2 and estado.con_la_anterior == 0
        assert _legibles_solo_con_la_actual(app) == (CLAVE_API, CONTRASENA_LDAP)

    def test_repetirlo_no_cambia_nada(self, clave_nueva):
        rotacion_service.recifrar()
        tokens = [c.api_key_encrypted for c in AIProviderConfig.query.all()]

        rotacion_service.recifrar()

        assert [c.api_key_encrypted for c in AIProviderConfig.query.all()] == tokens

    def test_sin_la_anterior_no_escribe_nada_y_lo_dice(self, app, guardado):
        app.config['SECRETS_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        antes = [c.api_key_encrypted for c in AIProviderConfig.query.all()]
        try:
            estado = rotacion_service.recifrar()

            assert len(estado.ilegibles) == 2
            assert any('proveedor de IA' in d for d in estado.ilegibles)
            assert any('LDAP_CONTRASENA' in d for d in estado.ilegibles)
            db.session.expire_all()
            assert [c.api_key_encrypted for c in AIProviderConfig.query.all()] == antes
        finally:
            app.config['SECRETS_ENCRYPTION_KEY'] = guardado

    def test_queda_en_la_auditoria(self, clave_nueva):
        from app.models.audit import AuditEvent

        rotacion_service.recifrar()

        evento = AuditEvent.query.filter_by(accion='crypto.key_rotated').one()
        assert evento.metadatos['recifrados'] == 2


@pytest.mark.integration
class TestLaOrdenDeLaConsola:
    def test_sin_la_clave_anterior_sale_con_error(self, app, guardado):
        app.config['SECRETS_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        try:
            resultado = app.test_cli_runner().invoke(args=['rotar-cifrado'])
        finally:
            app.config['SECRETS_ENCRYPTION_KEY'] = guardado

        assert resultado.exit_code == 3
        assert 'No se ha cambiado nada' in resultado.output

    def test_con_la_anterior_recifra_y_sale_bien(self, app, clave_nueva):
        resultado = app.test_cli_runner().invoke(args=['rotar-cifrado'])

        assert resultado.exit_code == 0
        assert rotacion_service.estado().con_la_anterior == 0


# ----------------------------------------------------------------------
# setup.sh --rotar, con un «docker» falso
# ----------------------------------------------------------------------
@pytest.fixture
def proyecto(tmp_path):
    for relativo in ('setup.sh', '.env.example', 'scripts/lib/proxy.sh',
                     'scripts/lib/tls.sh', 'docker/docker-compose.yml'):
        destino = tmp_path / relativo
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(RAIZ / relativo, destino)
    binarios = tmp_path / 'bin'
    binarios.mkdir()
    (binarios / 'docker').write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  --version) echo "Docker version 29.9.0" ;;\n'
        '  "volume inspect"*) exit 0 ;;\n'
        '  *"config --format json"*) echo \'{"name": "travel-manager"}\' ;;\n'
        '  *build*) echo "CONSTRUYENDO" ; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'
    )
    (binarios / 'docker').chmod(0o755)

    plantilla = (tmp_path / '.env.example').read_text().splitlines()
    valores = {
        'SECRET_KEY': 'sesion-vieja', 'SECRETS_ENCRYPTION_KEY': 'cifrado-viejo',
        'POSTGRES_PASSWORD': 'pg-vieja', 'REDIS_PASSWORD': 'redis-vieja',
        'S3_SECRET_KEY': 's3-vieja', 'METRICS_TOKEN': 'metricas-viejo',
    }
    lineas = []
    for linea in plantilla:
        clave = linea.split('=', 1)[0]
        lineas.append(f'{clave}={valores[clave]}' if clave in valores else linea)
    (tmp_path / 'docker' / '.env').write_text('\n'.join(lineas) + '\n')
    return tmp_path


def _setup(proyecto, *argumentos):
    entorno = dict(os.environ, PATH=f'{proyecto / "bin"}:{os.environ["PATH"]}')
    return subprocess.run(['bash', str(proyecto / 'setup.sh'), *argumentos], env=entorno,
                          capture_output=True, text=True, timeout=120)


def _env(proyecto):
    texto = (proyecto / 'docker' / '.env').read_text()
    return dict(
        linea.split('=', 1) for linea in texto.splitlines()
        if '=' in linea and not linea.startswith('#')
    )


@pytest.mark.unit
class TestSetupRotar:
    def test_genera_credenciales_nuevas_para_todo(self, proyecto):
        _setup(proyecto, '--rotar')

        env = _env(proyecto)
        for clave, vieja in (('SECRET_KEY', 'sesion-vieja'), ('POSTGRES_PASSWORD', 'pg-vieja'),
                             ('REDIS_PASSWORD', 'redis-vieja'), ('S3_SECRET_KEY', 's3-vieja'),
                             ('METRICS_TOKEN', 'metricas-viejo'),
                             ('SECRETS_ENCRYPTION_KEY', 'cifrado-viejo')):
            assert env[clave] and env[clave] != vieja, clave

    def test_la_clave_de_cifrado_vieja_pasa_a_anterior(self, proyecto):
        """Not discarded: what is stored is still encrypted with it."""
        _setup(proyecto, '--rotar')

        assert _env(proyecto)['SECRETS_ENCRYPTION_KEY_PREVIOUS'] == 'cifrado-viejo'

    def test_deja_las_viejas_en_la_copia(self, proyecto):
        _setup(proyecto, '--rotar')

        copia, = (proyecto / 'docker').glob('.env.*.bak')
        assert 'POSTGRES_PASSWORD=pg-vieja' in copia.read_text()

    def test_no_pisa_una_rotacion_a_medias(self, proyecto):
        """A previous key already set means one was not finished; overwriting
        it would lose what only it opens."""
        env = proyecto / 'docker' / '.env'
        env.write_text(env.read_text().replace('SECRETS_ENCRYPTION_KEY_PREVIOUS=',
                                               'SECRETS_ENCRYPTION_KEY_PREVIOUS=la-de-antes'))

        r = _setup(proyecto, '--rotar')

        assert r.returncode == 1
        assert 'no terminó' in r.stdout + r.stderr
        assert _env(proyecto)['SECRETS_ENCRYPTION_KEY'] == 'cifrado-viejo'

    def test_sin_rotar_no_cambia_ninguna(self, proyecto):
        antes = _env(proyecto)

        _setup(proyecto)

        despues = _env(proyecto)
        for clave in ('SECRET_KEY', 'POSTGRES_PASSWORD', 'SECRETS_ENCRYPTION_KEY'):
            assert despues[clave] == antes[clave], clave
