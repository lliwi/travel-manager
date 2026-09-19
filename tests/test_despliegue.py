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

    @pytest.mark.parametrize('servicio', ['postgres', 'redis', 'minio', 'clamav'])
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
