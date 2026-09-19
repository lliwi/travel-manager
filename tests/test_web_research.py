"""Public information retrieval: allow-list and SSRF hardening.

Specification sections 2.6 and 3.2. Every test here guards a control that stops
the research feature being used to reach somewhere it should not.
"""
import pytest

from app.extensions import db
from app.models.advisory import WebSource
from app.services import web_research_service as research
from app.utils.errors import SSRFBlocked, WebResearchError


@pytest.mark.security
class TestValidacionDeDirecciones:
    """Only public addresses are ever contacted."""

    @pytest.mark.parametrize('host', [
        '169.254.169.254',          # cloud metadata, the classic SSRF target
        'metadata.google.internal',
        'localhost',
        '127.0.0.1',
        '10.0.0.1',
        '192.168.1.1',
        '172.16.0.1',
        '0.0.0.0',
        '[::1]',
    ])
    def test_se_rechazan_los_destinos_internos(self, host):
        permitido, motivo = research.is_safe_address(host.strip('[]'))
        assert not permitido, f'{host} no debería estar permitido ({motivo}).'

    def test_se_acepta_una_direccion_publica(self):
        permitido, _ = research.is_safe_address('example.com')
        assert permitido

    def test_un_host_vacio_se_rechaza(self):
        assert not research.is_safe_address('')[0]
        assert not research.is_safe_address(None)[0]


@pytest.mark.security
class TestListaBlanca:
    """Nothing outside ``web_sources`` is fetched."""

    def test_un_dominio_de_la_lista_se_resuelve(self, seeded):
        fuente = research.allowed_source('https://www.exteriores.gob.es/es/algo')
        assert fuente is not None
        assert fuente.dominio == 'exteriores.gob.es'

    def test_un_subdominio_se_acepta(self, seeded):
        assert research.allowed_source('https://sede.exteriores.gob.es/x') is not None

    def test_un_dominio_que_solo_termina_igual_se_rechaza(self, seeded):
        """``malvadoexteriores.gob.es`` must not pass as ``exteriores.gob.es``."""
        assert research.allowed_source('https://malvadoexteriores.gob.es/x') is None

    def test_un_dominio_ajeno_se_rechaza(self, seeded):
        assert research.allowed_source('https://atacante.example/x') is None

    def test_un_esquema_no_http_se_rechaza(self, seeded):
        for url in ('file:///etc/passwd', 'gopher://x/', 'ftp://x/'):
            assert research.allowed_source(url) is None, url

    def test_una_fuente_desactivada_deja_de_valer(self, seeded):
        fuente = WebSource.query.filter_by(dominio='exteriores.gob.es').first()
        fuente.activa = False
        db.session.commit()

        assert research.allowed_source('https://exteriores.gob.es/x') is None

    def test_fetch_rechaza_un_destino_no_autorizado(self, app, seeded):
        # The feature is off in the testing configuration, which short-circuits
        # before the allow-list is even consulted. Enabled here so the refusal
        # under test is the allow-list's, not the feature flag's.
        app.config['WEB_RESEARCH_ENABLED'] = True
        try:
            with pytest.raises(SSRFBlocked, match='no está en la lista'):
                research.fetch('http://169.254.169.254/latest/meta-data/')

            with pytest.raises(SSRFBlocked):
                research.fetch('http://localhost:5000/admin')

            with pytest.raises(SSRFBlocked):
                research.fetch('file:///etc/passwd')
        finally:
            app.config['WEB_RESEARCH_ENABLED'] = False

    def test_fetch_falla_si_la_busqueda_esta_desactivada(self, app, seeded):
        app.config['WEB_RESEARCH_ENABLED'] = False
        try:
            with pytest.raises(WebResearchError, match='desactivada'):
                research.fetch('https://exteriores.gob.es/')
        finally:
            app.config['WEB_RESEARCH_ENABLED'] = True


@pytest.mark.security
class TestSaneamiento:
    """Retrieved content is stored as text, never as markup."""

    def test_se_eliminan_los_scripts(self):
        html = (
            '<html><head><title>Avisos</title>'
            '<script>fetch("http://atacante.example")</script>'
            '<style>body{color:red}</style></head>'
            '<body><p>Precaución en la zona norte.</p></body></html>'
        )
        texto = research._to_text(html)

        assert 'Precaución en la zona norte.' in texto
        assert 'atacante.example' not in texto
        assert '<script' not in texto
        assert 'color:red' not in texto

    def test_se_extrae_el_titulo(self):
        assert research._extract_title('<html><title>Avisos de viaje</title>') == \
            'Avisos de viaje'

    def test_se_decodifican_las_entidades(self):
        assert 'Precaución' in research._to_text('<p>Precauci&oacute;n</p>')

    def test_la_url_se_canonicaliza_para_la_cache(self):
        a = research._canonical('https://Exteriores.GOB.es/avisos/')
        b = research._canonical('https://exteriores.gob.es/avisos')
        assert a == b, 'El mismo recurso debe compartir entrada de caché.'


@pytest.mark.unit
class TestBusqueda:
    def test_la_busqueda_devuelve_vacio_si_esta_desactivada(self, app, seeded, trip):
        app.config['WEB_RESEARCH_ENABLED'] = False
        try:
            assert research.search('requisitos de entrada', trip=trip) == []
        finally:
            app.config['WEB_RESEARCH_ENABLED'] = True

    def test_una_fuente_inaccesible_no_rompe_la_busqueda(
        self, app, seeded, trip, monkeypatch
    ):
        """One unreachable source must not abort the whole consultation."""
        def falla(url, source=None, use_cache=True, actor=None):
            raise WebResearchError('la fuente no responde')

        monkeypatch.setattr(research, 'fetch', falla)

        assert research.search('requisitos de entrada', trip=trip) == []
