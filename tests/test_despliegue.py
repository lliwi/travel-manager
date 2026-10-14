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

    def test_el_dockerfile_las_aplica(self):
        texto = (RAIZ / 'docker' / 'flask' / 'Dockerfile').read_text()

        assert '-c constraints.txt' in texto
        assert 'COPY requirements.txt constraints.txt' in texto

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
