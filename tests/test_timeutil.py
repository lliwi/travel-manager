"""Timezone handling.

Every connection margin and every overlap check is computed from these
functions, so a mistake here is a mistake in every alert the system raises.
"""
from datetime import datetime

import pytest

from app.utils import timeutil


@pytest.mark.unit
class TestTripleDeInstante:
    """The local / tz / utc triple that every itinerary instant is stored as."""

    def test_set_instant_rellena_las_tres_columnas(self):
        entity = type('E', (), {})()
        timeutil.set_instant(entity, 'salida', datetime(2026, 6, 1, 10, 0), 'Europe/Madrid')

        assert entity.salida_local == datetime(2026, 6, 1, 10, 0)
        assert entity.salida_tz == 'Europe/Madrid'
        assert entity.salida_utc.hour == 8, 'Madrid está en UTC+2 en junio.'
        assert entity.salida_utc.tzinfo is not None

    def test_set_instant_con_none_limpia_las_tres(self):
        entity = type('E', (), {})()
        timeutil.set_instant(entity, 'salida', datetime(2026, 6, 1, 10, 0), 'Europe/Madrid')
        timeutil.set_instant(entity, 'salida', None, 'Europe/Madrid')

        assert entity.salida_local is None
        assert entity.salida_tz is None
        assert entity.salida_utc is None

    def test_una_zona_invalida_cae_a_utc_y_es_detectable(self):
        entity = type('E', (), {})()
        stored = timeutil.set_instant(
            entity, 'salida', datetime(2026, 6, 1, 10, 0), 'Nowhere/Fake'
        )

        assert stored == 'UTC'
        assert not timeutil.is_valid_timezone('Nowhere/Fake'), (
            'El llamante debe poder detectar que la zona no se resolvió y bajar '
            'la confianza del campo.'
        )

    def test_el_invierno_y_el_verano_dan_desfases_distintos(self):
        invierno = type('E', (), {})()
        verano = type('E', (), {})()
        timeutil.set_instant(invierno, 'salida', datetime(2026, 1, 15, 10, 0), 'Europe/Madrid')
        timeutil.set_instant(verano, 'salida', datetime(2026, 7, 15, 10, 0), 'Europe/Madrid')

        assert invierno.salida_utc.hour == 9, 'CET es UTC+1.'
        assert verano.salida_utc.hour == 8, 'CEST es UTC+2.'


@pytest.mark.unit
class TestMargenes:
    """Margins must be computed in UTC, never on the wall clock."""

    def test_un_vuelo_que_cruza_el_cambio_de_hora(self):
        """Madrid's clocks go forward on 29 March 2026.

        A flight leaving at 23:30 and landing at 03:15 looks like 3 h 45 on the
        clock, but one of those hours does not exist: the real duration is
        2 h 45. Storing only local times would get this wrong.
        """
        salida = timeutil.to_utc(datetime(2026, 3, 28, 23, 30), 'Europe/Madrid')
        llegada = timeutil.to_utc(datetime(2026, 3, 29, 3, 15), 'Europe/Madrid')

        assert timeutil.minutes_between(salida, llegada) == 165
        assert timeutil.crosses_dst_transition('Europe/Madrid', salida, llegada)

    def test_una_conexion_entre_zonas_distintas(self):
        """Landing 11:30 in London, departing 12:00 London: 30 minutes.

        Reading the Madrid arrival time against the London departure time would
        give a wrong answer in the dangerous direction.
        """
        llegada = timeutil.to_utc(datetime(2026, 6, 1, 11, 30), 'Europe/London')
        salida = timeutil.to_utc(datetime(2026, 6, 1, 12, 0), 'Europe/London')

        assert timeutil.minutes_between(llegada, salida) == 30

    def test_un_margen_negativo_indica_solapamiento(self):
        primero = timeutil.to_utc(datetime(2026, 6, 1, 14, 0), 'Europe/Madrid')
        segundo = timeutil.to_utc(datetime(2026, 6, 1, 13, 0), 'Europe/Madrid')

        assert timeutil.minutes_between(primero, segundo) == -60

    def test_el_desfase_entre_zonas(self):
        momento = timeutil.to_utc(datetime(2026, 6, 1, 12, 0), 'UTC')

        assert timeutil.timezone_shift_hours('Europe/Madrid', 'Europe/London', momento) == -1
        assert timeutil.timezone_shift_hours('Europe/Madrid', 'Asia/Tokyo', momento) == 7
        assert timeutil.timezone_shift_hours('Europe/Madrid', 'America/New_York', momento) == -6


@pytest.mark.unit
class TestSolapamientos:
    """Half-open intervals: touching is not overlapping."""

    def _utc(self, hour):
        return timeutil.to_utc(datetime(2026, 6, 1, hour, 0), 'UTC')

    def test_intervalos_que_se_solapan(self):
        assert timeutil.overlaps(self._utc(10), self._utc(14),
                                 self._utc(12), self._utc(16))

    def test_intervalos_que_solo_se_tocan(self):
        assert not timeutil.overlaps(self._utc(10), self._utc(12),
                                     self._utc(12), self._utc(14)), (
            'Un tramo que acaba cuando empieza el siguiente no se solapa.'
        )

    def test_intervalos_disjuntos(self):
        assert not timeutil.overlaps(self._utc(10), self._utc(11),
                                     self._utc(13), self._utc(14))

    def test_intervalo_contenido_en_otro(self):
        assert timeutil.overlaps(self._utc(10), self._utc(18),
                                 self._utc(12), self._utc(14))

    def test_con_valores_ausentes_no_hay_solape(self):
        assert not timeutil.overlaps(None, self._utc(14), self._utc(12), self._utc(16))


@pytest.mark.unit
class TestFormato:
    """Display helpers."""

    def test_formato_de_duracion(self):
        assert timeutil.format_duration(45) == '45 min'
        assert timeutil.format_duration(90) == '1 h 30 min'
        assert timeutil.format_duration(120) == '2 h'
        assert timeutil.format_duration(-30) == '-30 min'
        assert timeutil.format_duration(None) == '—'

    def test_formato_de_instante_con_zona(self):
        momento = timeutil.to_utc(datetime(2026, 6, 1, 10, 0), 'Europe/Madrid')
        rendered = timeutil.format_local(momento, 'Europe/Madrid')

        assert '01 jun 2026' in rendered
        assert '10:00' in rendered
        assert 'Madrid' in rendered
