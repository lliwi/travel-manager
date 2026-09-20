"""Reaching the internet from behind a corporate proxy.

Three things this has to get right, and only the first is obvious:

- Outbound traffic goes through the proxy when there is one.
- Internal traffic does **not**. Local inference is the case that matters: an
  Ollama beside the application would otherwise have every prompt routed
  through a machine with no reason to see it, and the same code path serves a
  local vLLM and a hosted OpenAI, so the bypass cannot depend on which.
- There is one precedence rule, stated: Ajustes wins, the environment is the
  fallback. Two sources with no order is how a setting ends up ignored by
  something nobody thought to check.
"""
import httpx
import pytest

from app.services import settings_service
from app.utils import http


def _destino(cliente, url):
    """Which proxy, if any, this client would use for a URL."""
    transporte = cliente._transport_for_url(httpx.URL(url))
    pool = getattr(transporte, '_pool', None)
    proxy = getattr(pool, '_proxy_url', None)
    if proxy is None:
        return None
    host = proxy.host
    return host.decode() if isinstance(host, bytes) else str(host)


@pytest.mark.unit
class TestSinProxyTodoSigueIgual:
    def test_por_defecto_se_sale_directo(self, app, seeded):
        with http.cliente() as cliente:
            assert _destino(cliente, 'https://serpapi.com/search') is None

    def test_se_dice_que_no_hay_proxy(self, app, seeded, monkeypatch):
        monkeypatch.delenv('HTTPS_PROXY', raising=False)
        monkeypatch.delenv('https_proxy', raising=False)

        assert 'Directamente' in http.descripcion()


@pytest.mark.integration
class TestConProxyConfigurado:
    def test_lo_externo_pasa_por_el(self, app, seeded):
        settings_service.set_value('PROXY_SALIDA', 'http://proxy.corp.local:3128')

        with http.cliente() as cliente:
            assert _destino(cliente, 'https://serpapi.com/search') == 'proxy.corp.local'

    def test_las_fuentes_publicas_tambien(self, app, seeded):
        settings_service.set_value('PROXY_SALIDA', 'http://proxy.corp.local:3128')

        with http.cliente() as cliente:
            destino = _destino(cliente, 'https://www.exteriores.gob.es/x')

        assert destino == 'proxy.corp.local'

    def test_un_proveedor_de_ia_externo_tambien(self, app, seeded):
        settings_service.set_value('PROXY_SALIDA', 'http://proxy.corp.local:3128')

        with http.cliente() as cliente:
            assert _destino(cliente, 'https://api.openai.com/v1/chat') == 'proxy.corp.local'


@pytest.mark.security
class TestLoInternoNoSaleAlProxy:
    """The failure this prevents is quiet: it works, slowly, and a machine
    outside the perimeter sees traffic that never had to leave it."""

    def _con_proxy(self):
        settings_service.set_value('PROXY_SALIDA', 'http://proxy.corp.local:3128')
        return http.cliente()

    def test_la_inferencia_local_va_directa(self, app, seeded):
        """The one that matters: Ollama runs beside the application and its
        prompts are the whole content of the documents."""
        with self._con_proxy() as cliente:
            destino = _destino(cliente, 'http://host.docker.internal:11434/api/chat')

        assert destino is None

    def test_localhost_tambien(self, app, seeded):
        with self._con_proxy() as cliente:
            assert _destino(cliente, 'http://localhost:11434/api/tags') is None
            assert _destino(cliente, 'http://127.0.0.1:9000/minio') is None

    def test_los_demas_contenedores_tambien(self, app, seeded):
        with self._con_proxy() as cliente:
            assert _destino(cliente, 'http://minio:9000/bucket') is None
            assert _destino(cliente, 'http://openldap:389/') is None

    def test_se_pueden_añadir_excepciones(self, app, seeded):
        """For an inference server elsewhere on the corporate network."""
        settings_service.set_value('PROXY_EXCEPCIONES', 'ia.corp.local, otro.corp.local')

        with self._con_proxy() as cliente:
            assert _destino(cliente, 'http://ia.corp.local:8000/v1/chat') is None
            assert _destino(cliente, 'https://serpapi.com/search') == 'proxy.corp.local'


@pytest.mark.integration
class TestLaPrecedenciaEstaDicha:
    def test_ajustes_gana_al_entorno(self, app, seeded, monkeypatch):
        monkeypatch.setenv('HTTPS_PROXY', 'http://del-entorno:3128')
        settings_service.set_value('PROXY_SALIDA', 'http://de-ajustes:8080')

        with http.cliente() as cliente:
            assert _destino(cliente, 'https://serpapi.com/x') == 'de-ajustes'

    def test_sin_ajuste_se_usa_el_entorno(self, app, seeded, monkeypatch):
        """Whoever builds the image will already have set these, and ignoring
        them would be a surprise in the expensive direction."""
        monkeypatch.setenv('HTTPS_PROXY', 'http://del-entorno:3128')
        settings_service.set_value('PROXY_SALIDA', '')

        with http.cliente() as cliente:
            assert _destino(cliente, 'https://serpapi.com/x') == 'del-entorno'

    def test_el_no_proxy_del_entorno_se_respeta(self, app, seeded, monkeypatch):
        monkeypatch.setenv('HTTPS_PROXY', 'http://del-entorno:3128')
        monkeypatch.setenv('NO_PROXY', 'interno.test')
        settings_service.set_value('PROXY_SALIDA', '')

        with http.cliente() as cliente:
            assert _destino(cliente, 'https://interno.test/x') is None


@pytest.mark.security
class TestLaContrasenaDelProxyNoSeEnsena:
    def _con_credenciales(self):
        settings_service.set_value('PROXY_SALIDA', 'http://proxy.corp.local:3128')
        settings_service.set_value('PROXY_USUARIO', 'usuario')
        settings_service.set_value('PROXY_CONTRASENA', 'secretisimo')

    def test_se_usa_para_autenticarse(self, app, seeded):
        self._con_credenciales()

        assert 'secretisimo' in http.proxy_configurado()

    def test_se_guarda_cifrada(self, app, seeded):
        from app.models.settings import SystemSetting

        self._con_credenciales()

        fila = SystemSetting.query.filter_by(clave='PROXY_CONTRASENA').first()
        assert 'secretisimo' not in str(fila.valor)

    def test_se_oculta_al_describirlo(self, app, seeded):
        """The description reaches a screen and the logs."""
        self._con_credenciales()

        texto = http.descripcion()

        assert 'secretisimo' not in texto
        assert 'usuario' in texto and 'proxy.corp.local' in texto

    def test_no_se_devuelve_a_la_pantalla_de_ajustes(self, as_user, admin, seeded):
        self._con_credenciales()

        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'secretisimo' not in html


@pytest.mark.unit
class TestTodaSalidaPasaPorAqui:
    """A barrier. Adding an outbound call with `httpx.Client` directly would
    work everywhere except behind a proxy, and nothing would say so until a
    deployment that has one."""

    def test_ningun_servicio_abre_su_propio_cliente(self):
        from pathlib import Path

        raiz = Path(__file__).resolve().parent.parent / 'app'
        culpables = []
        for fichero in raiz.rglob('*.py'):
            if fichero.name == 'http.py' and fichero.parent.name == 'utils':
                continue
            if 'httpx.Client(' in fichero.read_text():
                culpables.append(str(fichero.relative_to(raiz)))

        assert not culpables, (
            'Estos módulos construyen su propio cliente HTTP y no pasarían por '
            'el proxy:\n  ' + '\n  '.join(sorted(culpables))
        )

    def test_las_excepciones_cubren_lo_interno(self):
        """Losing one of these means that traffic starts leaving the perimeter
        the day somebody configures a proxy."""
        for host in ('localhost', '127.0.0.1', 'host.docker.internal', 'minio'):
            assert host in http.EXCEPCIONES_POR_DEFECTO
