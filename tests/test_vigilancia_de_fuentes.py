"""An advisory is written once and then describes a moment.

Countries raise and lower their warnings between one trip and the next, so an
advisory nobody revisits is a description of the day it was generated,
presented as if it were current. These tests pin what «revisar» means: each
advisory remembers the exact state of the page it was written from, and a page
that has moved since is noticed.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.advisory import SecurityAdvisory
from app.models.enums import TripStatus
from app.services import advisory_service, trip_service
from app.utils.timeutil import set_instant

TEXTO_ORIGINAL = 'Reino Unido exige una ETA a los ciudadanos españoles.'
TEXTO_CAMBIADO = 'Reino Unido ha elevado el nivel de alerta terrorista a severo.'
URL = 'https://www.gov.uk/foreign-travel-advice'


@pytest.fixture
def viaje_con_recomendaciones(gestor, trip, seeded, monkeypatch):
    """A trip whose advisories were written from a known page."""
    trip_service.add_destination(
        gestor, trip, ciudad='Londres', pais_codigo='GB', pais_nombre='Reino Unido',
    )
    set_instant(trip, 'inicio', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
    set_instant(trip, 'fin', datetime(2026, 11, 2, 23, 5), 'Europe/Madrid')
    db.session.commit()

    _fuente(monkeypatch, TEXTO_ORIGINAL)
    advisory_service.generate_for_trip(gestor, trip)
    return trip


def _fuente(monkeypatch, texto):
    from app.services import ai_service, web_research_service

    monkeypatch.setattr(
        web_research_service, 'pagina_del_lugar', lambda *a, **k: {
            'url': URL, 'contenido': texto, 'es_oficial': True, 'fuente': 'gov.uk',
        },
    )
    monkeypatch.setattr(ai_service, 'analyze_risks', lambda *a, **k: {
        'riesgos': [{
            'titulo': 'Requisitos de entrada',
            'descripcion': texto,
            'nivel': 'precaucion', 'categoria': 'seguridad',
        }],
        'run_id': 'x',
    })


def _leer(monkeypatch, texto):
    """What the watcher will find when it re-reads the page."""
    from app.services import web_research_service

    monkeypatch.setattr(
        web_research_service, 'fetch',
        lambda url, **k: {'url': url, 'contenido': texto},
    )


@pytest.mark.unit
class TestLaHuellaDeUnaFuente:
    def test_el_mismo_texto_da_la_misma_huella(self):
        assert advisory_service.huella_de('abc') == advisory_service.huella_de('abc')

    def test_un_texto_distinto_da_otra(self):
        assert advisory_service.huella_de(TEXTO_ORIGINAL) != advisory_service.huella_de(
            TEXTO_CAMBIADO
        )

    def test_el_espaciado_no_cuenta_como_cambio(self):
        """A site that reflows its own markup has not changed its advice.

        A warning that cries every day is a warning nobody reads.
        """
        assert advisory_service.huella_de('Reino  Unido\n exige') == (
            advisory_service.huella_de('Reino Unido exige')
        )

    def test_una_pagina_vacia_no_tiene_huella(self):
        assert advisory_service.huella_de('') is None
        assert advisory_service.huella_de(None) is None


@pytest.mark.unit
class TestSeGuardaDeQuePaginaSeEscribio:
    def test_cada_recomendacion_recuerda_su_fuente(self, viaje_con_recomendaciones):
        advisory = SecurityAdvisory.query.filter_by(
            trip_id=viaje_con_recomendaciones.id, is_deleted=False,
            pais_codigo='GB',
        ).first()

        assert advisory is not None
        assert advisory.fuentes
        assert advisory.fuentes[0]['huella'] == advisory_service.huella_de(
            TEXTO_ORIGINAL
        )


@pytest.mark.unit
class TestCuandoLaFuenteCambia:
    def test_si_no_ha_cambiado_no_se_hace_nada(
        self, viaje_con_recomendaciones, monkeypatch
    ):
        _leer(monkeypatch, TEXTO_ORIGINAL)

        resumen = advisory_service.vigilar_fuentes(regenerar=False)

        assert resumen['revisados'] == 1
        assert resumen['cambiados'] == 0

    def test_un_cambio_se_detecta(self, viaje_con_recomendaciones, monkeypatch):
        _leer(monkeypatch, TEXTO_CAMBIADO)

        resumen = advisory_service.vigilar_fuentes(regenerar=False)

        assert resumen['cambiados'] == 1

    def test_el_cambio_queda_auditado(self, viaje_con_recomendaciones, monkeypatch):
        """Even when nothing is regenerated, somebody has to be able to see it."""
        from app.models.audit import AuditEvent

        _leer(monkeypatch, TEXTO_CAMBIADO)

        advisory_service.vigilar_fuentes(regenerar=False)

        evento = AuditEvent.query.filter_by(accion='advisory.source_changed').first()
        assert evento is not None
        assert URL in str(evento.metadatos)

    def test_se_regeneran_si_asi_esta_configurado(
        self, gestor, viaje_con_recomendaciones, monkeypatch
    ):
        _fuente(monkeypatch, TEXTO_CAMBIADO)
        _leer(monkeypatch, TEXTO_CAMBIADO)

        resumen = advisory_service.vigilar_fuentes(actor=gestor, regenerar=True)

        assert resumen['regenerados'] == 1
        vivas = SecurityAdvisory.query.filter_by(
            trip_id=viaje_con_recomendaciones.id, is_deleted=False,
        ).all()
        assert any(TEXTO_CAMBIADO in (a.contenido or '') for a in vivas)

    def test_una_fuente_ilegible_no_cuenta_como_cambio(
        self, viaje_con_recomendaciones, monkeypatch
    ):
        """«No pude leerla» no es «dice otra cosa»."""
        from app.services import web_research_service

        def _revienta(url, **kwargs):
            raise RuntimeError('502')

        monkeypatch.setattr(web_research_service, 'fetch', _revienta)

        resumen = advisory_service.vigilar_fuentes(regenerar=False)

        assert resumen['cambiados'] == 0


@pytest.mark.unit
class TestQueViajesSeRevisan:
    def test_un_viaje_terminado_no_se_revisa(
        self, gestor, viaje_con_recomendaciones, monkeypatch
    ):
        """Re-reading the sources for a journey that already happened spends
        model calls on advice nobody can act on."""
        _leer(monkeypatch, TEXTO_CAMBIADO)
        viaje_con_recomendaciones.estado = TripStatus.FINALIZADO
        db.session.commit()

        resumen = advisory_service.vigilar_fuentes(regenerar=False)

        assert resumen['revisados'] == 0

    def test_un_viaje_sin_recomendaciones_no_se_revisa(
        self, gestor, trip, seeded, monkeypatch
    ):
        _leer(monkeypatch, TEXTO_CAMBIADO)

        resumen = advisory_service.vigilar_fuentes(regenerar=False)

        assert resumen['revisados'] == 0

    def test_la_tarea_esta_programada(self):
        from app.tasks.celery_app import celery

        tareas = {c['task'] for c in celery.conf.beat_schedule.values()}

        assert 'app.tasks.maintenance.watch_advisory_sources' in tareas
