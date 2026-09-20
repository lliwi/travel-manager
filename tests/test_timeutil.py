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

    def test_una_hora_local_no_se_convierte_otra_vez(self):
        """A ``_local`` column is already the wall time on the ticket.

        Converting it as if it were UTC shifts it by the offset: a departure
        entered as 08:00 in Madrid would be shown as 10:00.
        """
        local = datetime(2026, 6, 1, 8, 0)

        assert '08:00' in timeutil.format_local(local, 'Europe/Madrid')
        assert '08:00' in timeutil.format_local_time(local, 'Europe/Madrid')

    def test_las_dos_columnas_muestran_la_misma_hora(self):
        """``_local`` and ``_utc`` are the same instant and must read alike."""
        local = datetime(2026, 6, 1, 8, 0)
        utc = timeutil.to_utc(local, 'Europe/Madrid')

        assert (timeutil.format_local(local, 'Europe/Madrid')
                == timeutil.format_local(utc, 'Europe/Madrid'))

    def test_un_instante_utc_si_se_convierte_a_su_zona(self):
        """An aware value is a real instant and must be localised."""
        utc = timeutil.to_utc(datetime(2026, 6, 1, 8, 0), 'Europe/Madrid')

        assert '15:00' in timeutil.format_local(utc, 'Asia/Tokyo'), (
            'Las 08:00 de Madrid son las 15:00 en Tokio.'
        )

    def test_la_fecha_tambien_respeta_la_hora_local(self):
        """Near midnight a spurious conversion would change the day."""
        local = datetime(2026, 6, 1, 23, 30)

        assert '01 jun 2026' in timeutil.format_local_date(local, 'Europe/Madrid')


@pytest.mark.unit
class TestFiltrosConCadenas:
    """Not every value reaching a template is still a datetime.

    An AI answer carries the moment its data was read as an ISO string, which
    the filter used to hand straight to the page: a full timestamp with
    microseconds and a UTC offset, in the middle of Spanish prose.
    """

    def test_se_formatea_una_marca_iso(self):
        from app.utils.timeutil import format_local

        assert format_local('2026-09-19T22:35:39.668979+00:00') != '—'
        assert '668979' not in format_local('2026-09-19T22:35:39.668979+00:00')

    def test_una_cadena_que_no_es_fecha_no_rompe(self):
        from app.utils.timeutil import format_local

        assert format_local('la semana que viene') == '—'


@pytest.mark.unit
class TestComparablesEntreMotores:
    """PostgreSQL returns these aware and SQLite naive.

    The same two columns then compare fine in production and raise in the
    tests, or the other way round -- which is how a policy rule that worked on
    the developer's machine blew up on the first real trip.
    """

    def test_una_marca_sin_zona_se_lee_como_utc(self):
        from datetime import UTC, datetime

        from app.utils.timeutil import as_aware_utc

        sin_zona = datetime(2026, 6, 1, 10, 0)

        assert as_aware_utc(sin_zona) == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

    def test_una_marca_con_zona_no_se_toca(self):
        from datetime import UTC, datetime

        from app.utils.timeutil import as_aware_utc

        con_zona = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)

        assert as_aware_utc(con_zona) is con_zona

    def test_se_pueden_restar_entre_si(self):
        from datetime import UTC, datetime

        from app.utils.timeutil import as_aware_utc

        naive = as_aware_utc(datetime(2026, 6, 1, 10, 0))
        aware = as_aware_utc(datetime(2026, 6, 2, 10, 0, tzinfo=UTC))

        assert (aware - naive).days == 1

    def test_none_sigue_siendo_none(self):
        from app.utils.timeutil import as_aware_utc

        assert as_aware_utc(None) is None
