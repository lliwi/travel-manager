"""Deployment topology.

These assert properties of the compose file rather than of the code. They exist
because a mistake here fails silently: the worker spent an afternoon unable to
reach the AI provider while every unit test passed, since the test suite
substitutes the provider and never crosses a network at all.
"""
import pathlib

import pytest
import yaml

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / 'docker' / 'docker-compose.yml'


@pytest.fixture(scope='module')
def compose():
    return yaml.safe_load(COMPOSE.read_text())


@pytest.mark.unit
class TestRedes:
    def test_la_red_backend_es_interna(self, compose):
        """The datastores must not be reachable from outside Docker."""
        assert compose['networks']['backend']['internal'] is True

    @pytest.mark.parametrize('servicio', ['postgres', 'redis', 's3', 'clamav'])
    def test_los_almacenes_no_publican_puertos_al_exterior(self, compose, servicio):
        puertos = compose['services'][servicio].get('ports') or []
        for p in puertos:
            assert str(p).startswith('127.0.0.1:'), (
                f'{servicio} publica {p} en todas las interfaces.'
            )

    @pytest.mark.parametrize('servicio', ['postgres', 'redis', 'clamav'])
    def test_los_almacenes_estan_solo_en_la_red_interna(self, compose, servicio):
        assert compose['services'][servicio]['networks'] == ['backend']

    def test_el_worker_tiene_salida_a_red(self, compose):
        """The worker calls the AI provider and fetches public sources.

        On ``backend`` alone it has no route off the Docker bridge -- not to the
        host, not to the LAN, not to the internet -- and every extraction fails
        with «Network is unreachable». Nothing in the application can detect
        that; it only shows up as documents that never leave «clasificado».
        """
        redes = compose['services']['worker']['networks']
        assert 'frontend' in redes, (
            'El worker necesita salida a red para llamar al proveedor de IA y '
            'consultar las fuentes públicas autorizadas.'
        )

    def test_el_worker_no_publica_puertos(self, compose):
        """Outbound access must not come with inbound exposure."""
        assert not compose['services']['worker'].get('ports')

    def test_web_y_worker_resuelven_el_anfitrion(self, compose):
        """Ollama runs on the host, so both need `host.docker.internal`."""
        for servicio in ('web', 'worker'):
            hosts = compose['services'][servicio].get('extra_hosts') or []
            assert any('host.docker.internal' in h for h in hosts), (
                f'{servicio} no puede resolver el anfitrión.'
            )

    def test_solo_nginx_se_publica_al_exterior(self, compose):
        expuestos = [
            nombre for nombre, s in compose['services'].items()
            if any(not str(p).startswith('127.0.0.1:') for p in (s.get('ports') or []))
        ]
        assert expuestos == ['nginx'], (
            f'Estos servicios se publican al exterior: {expuestos}'
        )


@pytest.mark.unit
class TestConfiguracionDeIA:
    """Specification section 2.5: AI is configured in the panel, not the env."""

    def test_el_entorno_no_fija_proveedor_ni_modelo(self, compose):
        entorno = compose['services']['web'].get('environment') or {}
        prohibidas = {
            'AI_DEFAULT_PROVIDER', 'OLLAMA_BASE_URL', 'OLLAMA_DEFAULT_MODEL',
            'AI_ALLOW_EXTERNAL_DOCUMENTS', 'AI_ALLOW_EXTERNAL_PII',
        }
        presentes = prohibidas & set(entorno)
        assert not presentes, (
            f'Estas variables deben configurarse en el panel, no en el entorno: '
            f'{sorted(presentes)}'
        )

    def test_la_plantilla_de_entorno_tampoco_las_trae(self):
        plantilla = (COMPOSE.parent.parent / '.env.example').read_text()
        for variable in ('AI_DEFAULT_PROVIDER=', 'OLLAMA_BASE_URL=',
                         'OLLAMA_DEFAULT_MODEL='):
            assert variable not in plantilla, (
                f'{variable} sigue en .env.example; se configura en el panel.'
            )


@pytest.mark.unit
class TestSondasDeSalud:
    """The probes must answer however often they are asked.

    The container healthcheck runs every 30 seconds — 120 requests an hour
    against a default allowance of 100. Left rate-limited, a perfectly healthy
    container starts reporting itself unhealthy after an hour.
    """

    def test_healthz_no_esta_limitado(self, app, client):
        app.config['RATELIMIT_ENABLED'] = True
        try:
            for _ in range(150):
                assert client.get('/healthz').status_code == 200
        finally:
            app.config['RATELIMIT_ENABLED'] = False

    def test_las_dos_sondas_estan_exentas(self, app):
        """Checked against the limiter's own registry rather than by hammering.

        ``/readyz`` touches PostgreSQL and Redis, so calling it a hundred times
        would test the fixtures' patience more than the exemption.
        """
        from app.extensions import limiter

        # The registry keys by qualified name, so a substring match is what
        # identifies the view.
        exentas = ' '.join(limiter._route_exemptions)
        for sonda in ('healthz', 'readyz'):
            assert f'.{sonda}' in exentas, f'La sonda {sonda} está sujeta al límite.'

    def test_el_intervalo_del_healthcheck_cabe_en_el_limite(self, compose):
        """A guard on the arithmetic, in case either side is tuned later."""
        import re

        prueba = compose['services']['web']['healthcheck']['interval']
        segundos = int(re.match(r'(\d+)', str(prueba)).group(1))
        por_hora = 3600 / segundos

        assert por_hora > 100, (
            'Si esto deja de ser cierto, revise si la exención sigue haciendo '
            'falta; mientras lo sea, es imprescindible.'
        )


@pytest.mark.unit
class TestElAlmacenDeDocumentos:
    """SeaweedFS replaced MinIO when MinIO stopped publishing images.

    Each of these fails silently: the stack starts, and the problem shows up
    as documents anybody can read, a store that never reports healthy, or one
    that refuses uploads with the disk half empty.
    """

    def test_la_imagen_va_fijada_a_una_version(self, compose):
        imagen = compose['services']['s3']['image']
        etiqueta = imagen.rsplit(':', 1)[-1]

        assert ':' in imagen and etiqueta not in ('latest', 'dev'), imagen

    def test_las_credenciales_son_obligatorias(self, compose):
        """Without them SeaweedFS answers anonymous S3 requests."""
        entorno = compose['services']['s3']['environment']

        for variable in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
            assert ':?' in str(entorno[variable]), (
                f'{variable} tiene un valor por defecto: el almacén quedaría '
                f'abierto si faltara en docker/.env.'
            )

    def test_la_comprobacion_de_salud_no_usa_localhost(self, compose):
        """localhost is ::1 in the container and SeaweedFS does not listen there."""
        prueba = ' '.join(compose['services']['s3']['healthcheck']['test'])

        assert 'localhost' not in prueba
        assert '127.0.0.1' in prueba

    def test_el_numero_de_volumenes_lo_decide_el_disco(self, compose):
        """The default cap of 8 refuses uploads long before the disk is full."""
        assert '-volume.max=0' in compose['services']['s3']['command']

    def test_la_aplicacion_apunta_al_servicio(self, compose):
        for servicio in ('web', 'worker'):
            entorno = compose['services'][servicio]['environment']
            assert entorno['S3_ENDPOINT_URL'] == 'http://s3:9000', servicio


RAIZ = COMPOSE.parent.parent


def _nombre(requisito):
    """«Flask-SQLAlchemy>=3.1» -> «flask-sqlalchemy», as pip compares them."""
    import re

    nombre = re.split(r'[<>=!~\[; ]', requisito.strip(), maxsplit=1)[0]
    return re.sub(r'[-_.]+', '-', nombre).lower()


@pytest.mark.unit
class TestLaImagenEsLaQueSeProbo:
    """Found in production: requirements.txt only sets minimums, a build weeks
    later installed SQLAlchemy 2.1, whose default PostgreSQL driver is one we
    do not install, and the application could not start.
    """

    def _lineas(self, fichero):
        return [
            linea.strip() for linea in (RAIZ / fichero).read_text().splitlines()
            if linea.strip() and not linea.lstrip().startswith('#')
        ]

    def test_cada_dependencia_tiene_version_exacta(self):
        fijadas = {_nombre(linea): linea for linea in self._lineas('constraints.txt')}
        sueltas = [
            requisito for requisito in self._lineas('requirements.txt')
            if _nombre(requisito) not in fijadas
        ]

        assert not sueltas, (
            'Sin versión exacta en constraints.txt: ' + ', '.join(sueltas)
        )

    def test_las_versiones_son_exactas(self):
        inexactas = [linea for linea in self._lineas('constraints.txt') if '==' not in linea]

        assert not inexactas, inexactas

    @pytest.mark.parametrize('imagen', ['flask', 'celery'])
    def test_cada_dockerfile_las_aplica(self, imagen):
        """Both: worker and beat run the same code, and the Celery image was
        left installing whatever was newest when only web was fixed."""
        texto = (RAIZ / 'docker' / imagen / 'Dockerfile').read_text()

        assert '-c constraints.txt' in texto
        assert 'COPY requirements.txt constraints.txt' in texto

    def test_cada_dockerfile_de_python_esta_cubierto(self):
        """A new Python image must not be added without its pins."""
        for dockerfile in (RAIZ / 'docker').glob('*/Dockerfile'):
            texto = dockerfile.read_text()
            if 'pip install' in texto:
                assert '-c constraints.txt' in texto, dockerfile.parent.name

    def test_la_url_de_la_base_de_datos_nombra_el_controlador(self, monkeypatch):
        """Not SQLAlchemy's default: it is what changed under us."""
        from app.config import _database_url

        monkeypatch.delenv('DATABASE_URL', raising=False)
        assert _database_url().startswith('postgresql+psycopg2://')


@pytest.mark.unit
class TestLaContrasenaDePostgres:
    """Found in production: «password authentication failed» with the right
    password in docker/.env."""

    def test_no_va_pegada_en_una_url(self, compose):
        """Pasted into a URL, «@», «/» or «#» in it broke the URL."""
        for servicio in ('web', 'worker'):
            entorno = compose['services'][servicio]['environment']
            assert 'DATABASE_URL' not in entorno, servicio
            assert ':?' in str(entorno['POSTGRES_PASSWORD']), servicio

    @pytest.mark.parametrize('contrasena', ['a@b/c#d%e:f?g', 'sin-nada-raro'])
    def test_se_escapa_al_construir_la_url(self, monkeypatch, contrasena):
        from sqlalchemy.engine import make_url

        from app.config import _database_url

        monkeypatch.delenv('DATABASE_URL', raising=False)
        monkeypatch.setenv('POSTGRES_PASSWORD', contrasena)

        url = make_url(_database_url())
        assert url.password == contrasena
        assert url.host == 'postgres'

    def test_una_contrasena_rechazada_se_explica(self):
        """Not a traceback repeated every few seconds."""
        texto = (RAIZ / 'docker' / 'flask' / 'entrypoint.sh').read_text()

        assert 'password authentication failed' in texto
        assert 'ALTER USER' in texto


@pytest.mark.unit
class TestElScriptDeArranque:
    """start.sh, revisado tras parar y arrancar en producción."""

    TEXTO = (RAIZ / 'start.sh').read_text()

    def test_parar_ve_tambien_los_contenedores_caidos(self):
        """Without «-a», a web container in a restart loop was invisible, so
        «--stop» did nothing and left it for the next start."""
        assert 'compose ps -a -q' in self.TEXTO

    def test_un_cambio_de_versiones_reconstruye(self):
        assert '"${ROOT}/constraints.txt"' in self.TEXTO

    def test_una_migracion_fallida_no_se_da_por_buena(self):
        """«flask db upgrade | grep -q» reported a failed migration as «al día»."""
        assert 'flask db upgrade 2>&1 | grep' not in self.TEXTO
        assert 'Las migraciones han fallado' in self.TEXTO


@pytest.mark.unit
class TestUnaConstruccionFallidaDiceQueProbar:
    def test_explica_el_fallo_de_overlay(self, tmp_path):
        import subprocess

        (tmp_path / 'docker').mkdir()
        (tmp_path / 'docker' / '.env').write_text('')
        guion = (
            f'ROOT="{tmp_path}"; ENV_FILE="{tmp_path}/docker/.env"\n'
            'info() { :; }; warn() { :; }; fail() { :; }\n'
            'compose() { return 1; }\n'
            f'source "{RAIZ}/scripts/lib/proxy.sh"\n'
            'construir_imagenes; echo "rc=$?"\n'
        )
        r = subprocess.run(['bash', '-c', guion], capture_output=True, text=True,
                           env={'PATH': '/usr/bin:/bin'}, timeout=30)

        assert 'rc=1' in r.stdout
        assert 'docker builder prune -af' in r.stderr
        assert 'modprobe overlay' in r.stderr


@pytest.mark.unit
class TestLasComprobacionesDeSalud:
    def test_ninguna_usa_localhost(self, compose):
        """Found twice: localhost is ::1 in these containers, SeaweedFS and
        Nginx listen on IPv4 only, and the check failed with the service
        working -- «unhealthy» Nginx after every clean install."""
        culpables = [
            nombre for nombre, servicio in compose['services'].items()
            if 'localhost' in ' '.join(map(str, (servicio.get('healthcheck') or {}).get('test', [])))
        ]

        assert not culpables, culpables


@pytest.mark.unit
class TestHttpOHttps:
    """Visto al validar una instalación limpia: con el certificado autofirmado
    start.sh servía HTTP sin redirigir y la cookie de sesión seguía siendo
    solo-HTTPS, así que por http:// no se podía iniciar sesión."""

    def _decidir(self, tmp_path, con_certificado):
        import subprocess

        certs = tmp_path / 'docker' / 'nginx' / 'certs'
        certs.mkdir(parents=True)
        if con_certificado:
            subprocess.run(
                ['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '1',
                 '-keyout', str(certs / 'server.key'), '-out', str(certs / 'server.crt'),
                 '-subj', '/CN=prueba'],
                check=True, capture_output=True,
            )
        guion = (
            f'ROOT="{tmp_path}"\n'
            'ok() { echo "OK $*"; }; info() { :; }; warn() { echo "WARN $*"; }\n'
            f'source "{RAIZ}/scripts/lib/tls.sh"\n'
            'configurar_tls; echo "FORCE_HTTPS=$FORCE_HTTPS"\n'
        )
        r = subprocess.run(['bash', '-c', guion], capture_output=True, text=True, timeout=30)
        redireccion = (tmp_path / 'docker' / 'nginx' / 'tls_redirect.conf').read_text()
        return r.stdout, redireccion

    def test_con_el_autofirmado_todo_va_por_https(self, tmp_path):
        import shutil

        if not shutil.which('openssl'):
            pytest.skip('openssl no disponible')
        salida, redireccion = self._decidir(tmp_path, con_certificado=True)

        assert 'FORCE_HTTPS=true' in salida
        assert 'return 301 https://' in redireccion

    def test_sin_certificado_http_y_lo_dice(self, tmp_path):
        salida, redireccion = self._decidir(tmp_path, con_certificado=False)

        assert 'FORCE_HTTPS=false' in salida
        assert 'return 301' not in redireccion
        assert 'WARN' in salida

    @pytest.mark.parametrize('forzar, segura', [('true', True), ('false', False)])
    def test_la_cookie_sigue_a_la_decision(self, monkeypatch, forzar, segura):
        """A «Secure» cookie over HTTP is dropped, and nobody can sign in."""
        import importlib

        # By name: «import app.config» returns the «config» dict the package
        # re-exports, not the module.
        configuracion = importlib.import_module('app.config')

        monkeypatch.setenv('FORCE_HTTPS', forzar)
        recargada = importlib.reload(configuracion)
        try:
            assert recargada.ProductionConfig.SESSION_COOKIE_SECURE is segura
            assert recargada.ProductionConfig.REMEMBER_COOKIE_SECURE is segura
        finally:
            monkeypatch.delenv('FORCE_HTTPS')
            importlib.reload(configuracion)

    @pytest.mark.parametrize('script', ['setup.sh', 'start.sh'])
    def test_los_dos_scripts_deciden_igual(self, script):
        texto = (RAIZ / script).read_text()

        assert 'scripts/lib/tls.sh' in texto
        assert 'configurar_tls' in texto


@pytest.mark.unit
class TestLaRedireccionNoTumbaANginx:
    def test_el_fragmento_no_vive_en_conf_d(self):
        """nginx.conf loads conf.d/*.conf at http level, where «return» is not
        allowed: with any certificate the redirect stopped Nginx starting."""
        for fichero in ('docker/nginx/Dockerfile', 'docker/docker-compose.yml',
                        'docker/nginx/app.conf'):
            texto = (RAIZ / fichero).read_text()
            assert 'conf.d/tls_redirect.conf' not in texto, fichero
            assert 'snippets/tls_redirect.conf' in texto, fichero


@pytest.mark.unit
class TestArrancarSinCambiosNoReconstruye:
    """Visto al parar y arrancar una instalación limpia: los scripts escriben
    ficheros en docker/ al arrancar, y cada arranque se veía «con cambios»."""

    TEXTO = (RAIZ / 'start.sh').read_text()

    @pytest.mark.parametrize('generado', [
        'docker/.env*', 'docker/.entorno', 'docker/nginx/tls_redirect.conf',
        'docker/nginx/certs*',
    ])
    def test_lo_que_escriben_los_scripts_no_cuenta_como_cambio(self, generado):
        assert f'! -path "${{ROOT}}/{generado}"' in self.TEXTO

    def test_el_resumen_no_promete_http_cuando_todo_va_por_https(self):
        assert 'http://localhost:${puerto}"' not in self.TEXTO
        assert "https://%s" in self.TEXTO
