"""Dates shown as dd/mm/aaaa, and spans of days chosen in one calendar.

A native date input is drawn in the browser's language, so one set to English
showed mm/dd/yyyy in a Spanish application. The picker is served from here,
and the pages only have to say which fields are a range.
"""
import pytest

pytestmark = pytest.mark.integration


def _html(as_user, actor, url):
    with as_user(actor) as client:
        respuesta = client.get(url)
    assert respuesta.status_code == 200
    return respuesta.get_data(as_text=True)


class TestElSelectorSeSirveDesdeAqui:
    def test_la_pagina_lo_carga_con_su_traduccion(self, as_user, gestor):
        html = _html(as_user, gestor, '/trips/nuevo')

        assert 'vendor/flatpickr/flatpickr.min.js' in html
        assert 'vendor/flatpickr/es.js' in html
        assert 'vendor/flatpickr/flatpickr.min.css' in html


class TestLosRangosDeDias:
    @pytest.mark.parametrize('url, inicio, fin', [
        ('/trips/nuevo', 'inicio_local', 'fin_local'),
        ('/trips/planificar', 'ida', 'vuelta'),
    ])
    def test_el_inicio_dice_cual_es_su_fin(self, as_user, gestor, seeded, url, inicio, fin):
        html = _html(as_user, gestor, url)

        assert f'data-rango-fin="{fin}"' in html
        assert 'data-hora-inicio="00:00"' in html and 'data-hora-fin="23:59"' in html

    def test_destinos_y_personas_tambien(self, as_user, gestor, trip):
        assert 'data-rango-fin="fin_local"' in _html(as_user, gestor,
                                                     f'/trips/{trip.id}/destinos')
        assert 'data-rango-fin="hasta_local"' in _html(as_user, gestor,
                                                       f'/trips/{trip.id}/viajeros')

    def test_en_la_planificacion_la_vuelta_es_opcional(self, as_user, gestor, seeded):
        assert 'data-rango-abierto' in _html(as_user, gestor, '/trips/planificar')

    def test_los_tramos_con_hora_no_son_un_rango(self, as_user, gestor, trip):
        """A flight leaves at an hour: that form keeps both fields, with time."""
        html = _html(as_user, gestor, f'/trips/{trip.id}/itinerario/segmento/nuevo')

        assert 'data-rango-fin' not in html
        assert 'type="datetime-local"' in html

    def test_lo_que_envia_el_rango_lo_acepta_el_servidor(self, as_user, gestor):
        from app.models.trip import Trip

        with as_user(gestor) as client:
            client.post('/trips/nuevo', data={
                'titulo': 'Rango', 'estado': 'borrador',
                'inicio_local': '2026-10-28T00:00', 'inicio_tz': 'Europe/Madrid',
                'fin_local': '2026-10-31T23:59', 'fin_tz': 'Europe/Madrid',
            })

        viaje = Trip.query.filter_by(titulo='Rango').one()
        assert viaje.fin_local.isoformat() == '2026-10-31T23:59:00'
