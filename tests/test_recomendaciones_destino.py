"""A security advisory must be about the place the traveller is going.

Taken from a real trip to London. The research service fetched the official
index of travel advice -- 394.944 characters listing every country -- and the
first six thousand of them are the top of an alphabetical list. The model read
about Afghanistan, wrote about Afghanistan, and the result was filed under the
United Kingdom, at «alto riesgo», for a trip to Gatwick.

Two defences, because either alone would have let it through: the model is
given the part of the page that mentions the destination, or nothing; and an
answer about another country is refused rather than stored.
"""
import pytest

from app.extensions import db


@pytest.mark.unit
class TestElFragmentoQueSeEnvia:
    def _indice(self):
        """An index page, the way an official source really answers."""
        return (
            'Recomendaciones de viaje. Sede electrónica. Contacto. '
            + 'Afganistán: situación de extrema gravedad. ' * 40
            + 'x' * 5000
            + 'Reino Unido: se recomienda documentación en regla. '
            + 'z' * 5000
        )

    def test_se_recorta_por_donde_habla_del_destino(self):
        from app.services.web_research_service import fragmento_sobre

        fragmento = fragmento_sobre(self._indice(), ['Reino Unido', 'Londres'])

        assert 'Reino Unido' in fragmento
        assert 'Afganistán' not in fragmento, (
            'Coger el principio de la página es lo que produjo la recomendación '
            'sobre Afganistán.'
        )

    def test_una_pagina_que_no_habla_del_destino_no_aporta_nada(self):
        from app.services.web_research_service import fragmento_sobre

        assert fragmento_sobre('Afganistán, Angola, Argelia.', ['Reino Unido']) is None

    def test_sin_lugares_se_recorta_como_antes(self):
        from app.services.web_research_service import fragmento_sobre

        assert fragmento_sobre('a' * 10000, []) == 'a' * 6000

    def test_una_pagina_corta_se_envia_entera(self):
        from app.services.web_research_service import fragmento_sobre

        texto = 'Reino Unido: entrada con pasaporte.'

        assert fragmento_sobre(texto, ['Reino Unido']) == texto


@pytest.mark.unit
class TestNoSeGuardaLoQueHablaDeOtroPais:
    def _destino(self, gestor, trip):
        from app.services import trip_service

        return trip_service.add_destination(
            gestor, trip, ciudad='Londres', pais_codigo='GB',
            pais_nombre='Reino Unido',
        )

    def test_se_detecta_un_riesgo_sobre_otro_pais(self, gestor, trip, seeded):
        from app.services.advisory_service import _pais_ajeno

        destino = self._destino(gestor, trip)
        riesgo = {
            'titulo': 'Inestabilidad política y seguridad',
            'descripcion': (
                'Marruecos atraviesa una situación que aconseja extremar '
                'precauciones en determinadas zonas.'
            ),
        }

        assert _pais_ajeno(riesgo, destino) == 'Marruecos'

    def test_solo_reconoce_los_paises_del_catalogo(self, gestor, trip, seeded):
        """Stated, not assumed: this net has holes.

        The catalogue is a working set, not the full ISO list, so a country it
        does not hold passes this check. That is why it is the second defence
        and not the first -- the real Afganistán case is stopped upstream, by
        never handing the model a page that does not mention the destination.
        """
        from app.models.catalog import Country
        from app.services.advisory_service import _pais_ajeno

        destino = self._destino(gestor, trip)
        assert Country.query.filter_by(codigo='AF').first() is None

        riesgo = {'descripcion': 'Afganistán enfrenta una situación inestable.'}

        assert _pais_ajeno(riesgo, destino) is None

    def test_un_riesgo_sobre_el_destino_se_acepta(self, gestor, trip, seeded):
        from app.services.advisory_service import _pais_ajeno

        destino = self._destino(gestor, trip)
        riesgo = {
            'titulo': 'Requisitos de entrada',
            'descripcion': 'Para entrar en Reino Unido se exige ETA.',
        }

        assert _pais_ajeno(riesgo, destino) is None

    def test_un_riesgo_sin_pais_no_se_descarta(self, gestor, trip, seeded):
        """A risk about the itinerary itself names no country, and is fine."""
        from app.services.advisory_service import _pais_ajeno

        destino = self._destino(gestor, trip)
        riesgo = {
            'titulo': 'Margen de conexión ajustado',
            'descripcion': 'El enlace deja menos de una hora entre vuelos.',
        }

        assert _pais_ajeno(riesgo, destino) is None


@pytest.mark.unit
class TestDestinosDeducidosDelItinerario:
    """The itinerary already says where the trip goes.

    Until the destinations were typed by hand, the advisory generator had
    nothing to work from -- and what a manager would type is exactly what the
    approved bookings already say.
    """

    def _vuelo(self, trip, destino_codigo, destino_ciudad, destino_pais,
               origen_codigo='MAD'):
        from datetime import datetime

        from app.models.itinerary import TravelSegment
        from app.utils.timeutil import set_instant

        segmento = TravelSegment(
            trip_id=trip.id, numero='IB1',
            origen_codigo=origen_codigo,
            destino_codigo=destino_codigo,
            destino_ciudad=destino_ciudad,
            destino_pais=destino_pais,
        )
        set_instant(segmento, 'salida', datetime(2026, 6, 1, 10, 0), 'Europe/Madrid')
        set_instant(segmento, 'llegada', datetime(2026, 6, 1, 11, 30), 'Europe/London')
        db.session.add(segmento)
        db.session.commit()
        return segmento

    def test_el_destino_de_un_vuelo_se_convierte_en_destino(
        self, gestor, trip, seeded
    ):
        from app.services import trip_service

        self._vuelo(trip, 'LGW', 'London', 'GB')

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        assert len(creados) == 1
        assert creados[0].pais_codigo == 'GB'
        assert creados[0].pais_nombre, 'El nombre del país sale del catálogo.'

    def _vuelo_en(self, trip, destino_codigo, destino_pais, salida, llegada,
                  origen_codigo='MAD'):
        from app.models.itinerary import TravelSegment
        from app.utils.timeutil import set_instant

        segmento = TravelSegment(
            trip_id=trip.id, numero='IB1',
            origen_codigo=origen_codigo, destino_codigo=destino_codigo,
            destino_pais=destino_pais,
        )
        set_instant(segmento, 'salida', salida, 'Europe/Madrid')
        set_instant(segmento, 'llegada', llegada, 'Europe/Madrid')
        db.session.add(segmento)
        db.session.commit()
        return segmento

    def test_un_aeropuerto_de_paso_no_es_un_destino(self, gestor, trip, seeded):
        """Changing planes somewhere is not going there."""
        from datetime import datetime

        from app.services import trip_service

        self._vuelo_en(
            trip, 'LHR', 'GB',
            datetime(2026, 6, 1, 8, 0), datetime(2026, 6, 1, 10, 0),
        )
        self._vuelo_en(
            trip, 'JFK', 'US',
            datetime(2026, 6, 1, 12, 0), datetime(2026, 6, 1, 20, 0),
            origen_codigo='LHR',
        )

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        paises = {d.pais_codigo for d in creados}
        assert 'US' in paises
        assert 'GB' not in paises, 'Dos horas en LHR es un trasbordo.'

    def test_el_destino_de_una_ida_y_vuelta_no_es_una_escala(
        self, gestor, trip, seeded
    ):
        """The one place the trip is about was being thrown away.

        On a return booking the destination is always also where the journey
        home departs from, so «is the origin of a later leg» discarded London
        and left the trip with no country -- and the security advisories with
        nothing to look up. What separates a stopover from a stay is time.
        """
        from datetime import datetime

        from app.services import trip_service

        self._vuelo_en(
            trip, 'LGW', 'GB',
            datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20),
            origen_codigo='BCN',
        )
        self._vuelo_en(
            trip, 'BCN', 'ES',
            datetime(2026, 11, 2, 19, 55), datetime(2026, 11, 2, 23, 5),
            origen_codigo='LGW',
        )

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        assert 'GB' in {d.pais_codigo for d in creados}, (
            'Dos noches en Londres no son un trasbordo.'
        )
        assert 'ES' not in {d.pais_codigo for d in creados}, (
            'Volver a casa no es ir a un destino.'
        )

    def test_un_alojamiento_sin_pais_no_duplica_la_ciudad(
        self, gestor, trip, seeded
    ):
        """Two entries for one city, one of them with nothing to look up."""
        from datetime import datetime

        from app.models.itinerary import Accommodation
        from app.services import trip_service
        from app.utils.timeutil import set_instant

        self._vuelo_en(
            trip, 'LGW', 'GB',
            datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20),
            origen_codigo='BCN',
        )
        alojamiento = Accommodation(
            trip_id=trip.id, nombre='Zedwell', ciudad='London', pais=None,
        )
        set_instant(alojamiento, 'check_in', datetime(2026, 10, 31, 15, 0),
                    'Europe/London')
        db.session.add(alojamiento)
        db.session.commit()
        db.session.refresh(trip)

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        assert len(creados) == 1, (
            'Una ciudad escrita de dos maneras sigue siendo una ciudad.'
        )
        assert creados[0].pais_codigo == 'GB'
        assert creados[0].ciudad == 'Londres', (
            'Se guarda como la nombra el catálogo, que es lo que permite '
            'reconocerla la próxima vez.'
        )

    def test_no_se_duplica_un_destino_que_ya_existe(self, gestor, trip, seeded):
        from app.services import trip_service

        trip_service.add_destination(
            gestor, trip, ciudad='Londres', pais_codigo='GB',
        )
        self._vuelo(trip, 'LGW', 'London', 'GB')

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        assert creados == []

    def test_un_alojamiento_tambien_aporta_su_ciudad(self, gestor, trip, seeded):
        from datetime import datetime

        from app.models.itinerary import Accommodation
        from app.services import trip_service
        from app.utils.timeutil import set_instant

        alojamiento = Accommodation(
            trip_id=trip.id, nombre='Zedwell', ciudad='London', pais='GB',
        )
        set_instant(alojamiento, 'check_in', datetime(2026, 6, 1, 15, 0), 'Europe/London')
        db.session.add(alojamiento)
        db.session.commit()

        creados = trip_service.sync_destinations_from_itinerary(
            gestor, trip, commit=True,
        )

        assert [d.pais_codigo for d in creados] == ['GB']

    def test_no_se_toca_lo_que_puso_el_gestor(self, gestor, trip, seeded):
        """Only adds. A destination someone entered is theirs."""
        from app.services import trip_service

        propio = trip_service.add_destination(
            gestor, trip, ciudad='Edimburgo', pais_codigo='GB',
        )
        self._vuelo(trip, 'LGW', 'London', 'GB')

        trip_service.sync_destinations_from_itinerary(gestor, trip, commit=True)

        db.session.refresh(propio)
        assert propio.ciudad == 'Edimburgo'


@pytest.mark.unit
class TestNiSiquieraSeLePreguntaAlModelo:
    """The defence that needs no reference data.

    A source that never names the destination cannot support an advisory about
    it, so it is dropped before the model is called. With every source dropped,
    the generator records that it could not check -- which is what a manager
    needs to tell from "no risks found".
    """

    def test_un_indice_de_paises_no_llega_al_modelo(self, gestor, trip, seeded):
        from app.services import advisory_service, trip_service

        destino = trip_service.add_destination(
            gestor, trip, ciudad='Londres', pais_codigo='GB',
            pais_nombre='Reino Unido',
        )
        fuentes = [{
            'url': 'https://www.exteriores.gob.es/recomendaciones',
            'contenido': 'Afganistán. Albania. Alemania. Andorra. Angola.',
        }]

        assert advisory_service._fuentes_sobre(fuentes, destino) == []

    def test_una_fuente_que_habla_del_destino_si_llega(self, gestor, trip, seeded):
        from app.services import advisory_service, trip_service

        destino = trip_service.add_destination(
            gestor, trip, ciudad='Londres', pais_codigo='GB',
            pais_nombre='Reino Unido',
        )
        fuentes = [{
            'url': 'https://www.gov.uk/foreign-travel-advice',
            'contenido': 'Reino Unido exige una autorización electrónica ETA.',
        }]

        assert len(advisory_service._fuentes_sobre(fuentes, destino)) == 1


@pytest.mark.integration
class TestRegenerarRecomendaciones:
    """Regenerating says again what applies now; it does not add to what was.

    Each run used to append, so a trip consulted three times carried three
    copies of the same advice and a manager had to work out which run each one
    came from.
    """

    def _con_fuente(self, monkeypatch, texto='Reino Unido exige una ETA.'):
        from app.services import ai_service, web_research_service

        monkeypatch.setattr(
            web_research_service, 'pagina_del_lugar', lambda *a, **k: {
                'url': 'https://www.gov.uk/foreign-travel-advice',
                'contenido': texto, 'es_oficial': True, 'fuente': 'gov.uk',
            },
        )
        monkeypatch.setattr(ai_service, 'analyze_risks', lambda *a, **k: {
            'riesgos': [{
                'titulo': 'Autorización electrónica de viaje',
                'descripcion': 'Reino Unido exige una ETA antes de viajar.',
                'nivel': 'precaucion', 'categoria': 'seguridad',
            }],
            'run_id': 'x',
        })

    def _viaje_listo(self, gestor, trip):
        from datetime import datetime

        from app.services import trip_service
        from app.utils.timeutil import set_instant

        trip_service.add_destination(
            gestor, trip, ciudad='Londres', pais_codigo='GB',
            pais_nombre='Reino Unido',
        )
        set_instant(trip, 'inicio', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
        set_instant(trip, 'fin', datetime(2026, 11, 2, 23, 5), 'Europe/Madrid')
        db.session.commit()

    def test_la_segunda_generacion_no_duplica(self, gestor, trip, seeded, monkeypatch):
        from app.models.advisory import SecurityAdvisory
        from app.services import advisory_service

        self._viaje_listo(gestor, trip)
        self._con_fuente(monkeypatch)

        advisory_service.generate_for_trip(gestor, trip)
        primera = SecurityAdvisory.query.filter_by(
            trip_id=trip.id, is_deleted=False,
        ).count()

        advisory_service.generate_for_trip(gestor, trip)
        segunda = SecurityAdvisory.query.filter_by(
            trip_id=trip.id, is_deleted=False,
        ).count()

        assert primera == segunda, 'Regenerar sustituye, no acumula.'

    def test_las_anteriores_se_retiran_sin_destruirse(
        self, gestor, trip, seeded, monkeypatch
    ):
        """The trail still leads back to what was shown and who decided on it."""
        from app.models.advisory import SecurityAdvisory
        from app.services import advisory_service

        self._viaje_listo(gestor, trip)
        self._con_fuente(monkeypatch)

        advisory_service.generate_for_trip(gestor, trip)
        advisory_service.generate_for_trip(gestor, trip)

        retiradas = SecurityAdvisory.query.filter_by(
            trip_id=trip.id, is_deleted=True,
        ).all()

        assert retiradas
        assert all(a.deleted_by_id == gestor.id for a in retiradas)

    def test_nacen_validadas(self, gestor, trip, seeded, monkeypatch):
        """The manager's job is rejecting what does not apply."""
        from app.models.enums import AdvisoryValidationState
        from app.services import advisory_service

        self._viaje_listo(gestor, trip)
        self._con_fuente(monkeypatch)

        creadas = advisory_service.generate_for_trip(gestor, trip)

        reales = [a for a in creadas if a.pais_codigo == 'GB']
        assert reales
        assert all(
            a.estado_validacion is AdvisoryValidationState.VALIDADA for a in reales
        )

    def test_un_aviso_de_que_no_se_pudo_comprobar_sigue_pendiente(
        self, gestor, trip, seeded, monkeypatch
    ):
        """That notice is for the manager, not advice for the traveller.

        «No se pudo consultar ninguna fuente» is not a recommendation, and
        publishing it as one would put a non-advisory in front of whoever is
        travelling. It stays pending because it is exactly the thing a manager
        needs to act on.
        """
        from app.models.enums import AdvisoryValidationState
        from app.services import advisory_service, web_research_service

        self._viaje_listo(gestor, trip)
        monkeypatch.setattr(
            web_research_service, 'pagina_del_lugar', lambda *a, **k: None,
        )

        creadas = advisory_service.generate_for_trip(gestor, trip)

        assert creadas
        assert all(
            a.estado_validacion is AdvisoryValidationState.PENDIENTE_VALIDACION
            for a in creadas
        )

    def test_se_puede_exigir_validacion_previa(
        self, gestor, trip, seeded, monkeypatch
    ):
        """Section 2.7 is still available to an organisation that needs it."""
        from app.models.enums import AdvisoryValidationState
        from app.services import advisory_service, settings_service

        settings_service.set_value('RECOMENDACIONES_VALIDAR_AL_GENERAR', False)
        self._viaje_listo(gestor, trip)
        self._con_fuente(monkeypatch)

        creadas = advisory_service.generate_for_trip(gestor, trip)

        assert all(
            a.estado_validacion is AdvisoryValidationState.PENDIENTE_VALIDACION
            for a in creadas
        )

    def test_el_gestor_puede_rechazar_una(self, gestor, trip, seeded, monkeypatch):
        from app.models.enums import AdvisoryValidationState
        from app.services import advisory_service

        self._viaje_listo(gestor, trip)
        self._con_fuente(monkeypatch)
        creada = advisory_service.generate_for_trip(gestor, trip)[0]

        advisory_service.validate(
            gestor, creada, aprobar=False, comentario='No aplica a este viaje.',
        )

        assert creada.estado_validacion is AdvisoryValidationState.RECHAZADA
        assert creada.comentario_validacion == 'No aplica a este viaje.'


@pytest.mark.unit
class TestSeguirElEnlaceDelPais:
    """An index of every country says nothing about where the traveller goes.

    The official source answers with an alphabetical list and a link to each
    country's own page; the first six thousand characters of that list are the
    top of the alphabet, which is how a trip to London produced an advisory
    about Afghanistan. The link is followed instead -- discovered on the page,
    never composed, because a URL we invent rots the first time the site
    changes its shape.
    """

    #: How the real source publishes them: inside a JSON payload, not anchors.
    INDICE = '''
        <html><body><script>var paises = [
          {"Title":"Afganistán","url":"/es/ServiciosAlCiudadano/Paginas/Detalle-recomendaciones-de-viaje.aspx?trc=Afganist%c3%a1n"},
          {"Title":"Alemania","url":"/es/ServiciosAlCiudadano/Paginas/Detalle-recomendaciones-de-viaje.aspx?trc=Alemania"},
          {"Title":"Reino Unido","url":"/es/ServiciosAlCiudadano/Paginas/Detalle-recomendaciones-de-viaje.aspx?trc=Reino+Unido"}
        ];</script></body></html>
    '''

    def _enlace(self, nombres, seeded):
        from app.services.web_research_service import enlace_al_lugar

        return enlace_al_lugar(
            self.INDICE,
            'https://www.exteriores.gob.es/es/ServiciosAlCiudadano/Paginas/'
            'Recomendaciones-de-viaje.aspx',
            nombres,
        )

    def test_encuentra_la_pagina_del_pais(self, app, seeded):
        enlace = self._enlace(['Londres', 'Reino Unido', 'GB'], seeded)

        assert enlace is not None
        assert 'trc=Reino+Unido' in enlace
        assert enlace.startswith('https://www.exteriores.gob.es/')

    def test_no_confunde_un_pais_con_otro(self, app, seeded):
        enlace = self._enlace(['Berlín', 'Alemania', 'DE'], seeded)

        assert 'trc=Alemania' in enlace

    def test_ignora_un_codigo_de_dos_letras(self, app, seeded):
        """«GB» appears inside half the paths on any site."""
        assert self._enlace(['GB'], seeded) is None

    def test_un_pais_que_no_figura_no_da_enlace(self, app, seeded):
        assert self._enlace(['Kiribati'], seeded) is None

    def test_no_sigue_un_enlace_fuera_de_la_lista_blanca(self, app, seeded):
        """Following a link is still a fetch, and the allow-list still rules."""
        from app.services.web_research_service import enlace_al_lugar

        html = '<a href="https://ejemplo-no-autorizado.test/Reino-Unido">x</a>'

        assert enlace_al_lugar(
            html, 'https://www.exteriores.gob.es/', ['Reino Unido'],
        ) is None
