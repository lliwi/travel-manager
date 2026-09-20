"""Warning that nobody has checked in yet.

The booking exists from the moment somebody pays; the boarding pass only once
somebody checks in. Between the two there is a window in which the trip looks
finished and nothing has been done, and it turns out to be a problem at the
airport.

The direction of every doubt is the design, and it is what most of these tests
pin: failing to match a pass raises an alert for a flight already checked in
for, which is a nuisance. Matching the wrong one silences the alert for a
flight nobody has checked in for, which is the failure the rule exists to
catch.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models.enums import AlertSeverity, DocumentType, SegmentType
from app.services.alerts.rules.boarding import BoardingPassRule
from app.utils.timeutil import utcnow


def _contexto(trip, segmentos, documentos=(), travelers=(), horas=48):
    from app.services.alerts.base import TripContext

    class _Ajuste:
        def __init__(self, valores):
            self._valores = valores

        def get(self, clave, defecto=None):
            return self._valores.get(clave, defecto)

    return TripContext(
        trip=trip,
        segments=list(segmentos),
        accommodations=[], vehicles=[], services=[], destinations=[],
        travelers=list(travelers),
        settings={'sin_tarjeta_de_embarque': _Ajuste({'horas_antes': horas})},
        documents=list(documentos),
    )


@pytest.fixture
def vuelo(trip, segment_factory):
    """A flight leaving in a day: inside the 48-hour window."""
    from app.utils.timeutil import set_instant

    segmento = segment_factory(numero='IB3210', origen='BCN', destino='LHR')
    set_instant(segmento, 'salida', (utcnow() + timedelta(hours=24)).replace(tzinfo=None),
                'UTC')
    set_instant(segmento, 'llegada', (utcnow() + timedelta(hours=26)).replace(tzinfo=None),
                'UTC')
    db.session.commit()
    return segmento


def _documento(trip, gestor, tipo, texto='', traveler=None):
    """A document on the trip, with the text the matching reads."""
    from app.models.document import Document, DocumentText

    doc = Document(
        trip_id=trip.id,
        trip_traveler_id=traveler.id if traveler is not None else None,
        nombre_original='doc.pdf',
        tipo=tipo,
        subido_por_id=gestor.id,
        hash_sha256='0' * 64,
        tamano_bytes=10,
        mime='application/pdf',
    )
    db.session.add(doc)
    db.session.flush()
    db.session.add(DocumentText(document_id=doc.id, contenido=texto))
    db.session.commit()
    return doc


@pytest.mark.integration
class TestCuandoAvisa:
    def test_un_vuelo_proximo_sin_tarjeta(self, app, trip, vuelo):
        candidatos = BoardingPassRule().evaluate(_contexto(trip, [vuelo]))

        assert len(candidatos) == 1
        assert 'tarjeta de embarque' in candidatos[0].titulo.lower()

    def test_uno_lejano_todavia_no(self, app, trip, vuelo):
        """48 hours is when check-in opens. Asking before it does is asking
        somebody to do something they cannot do yet."""
        candidatos = BoardingPassRule().evaluate(_contexto(trip, [vuelo], horas=6))

        assert candidatos == []

    def test_el_umbral_es_configurable(self, app, trip, vuelo):
        assert BoardingPassRule().evaluate(_contexto(trip, [vuelo], horas=72))

    def test_uno_ya_salido_no(self, app, trip, segment_factory):
        """Whatever happened, happened. An alert about it now is a list item
        nobody can act on."""
        from app.utils.timeutil import set_instant

        pasado = segment_factory(numero='IB9999')
        set_instant(pasado, 'salida', (utcnow() - timedelta(hours=3)).replace(tzinfo=None), 'UTC')
        db.session.commit()

        assert BoardingPassRule().evaluate(_contexto(trip, [pasado])) == []

    def test_un_tren_no(self, app, trip, segment_factory):
        """A train ticket is the ticket. Demanding a boarding pass for one
        would be noise on every journey."""
        from app.utils.timeutil import set_instant

        tren = segment_factory(numero='AVE3110', tipo=SegmentType.TREN)
        set_instant(tren, 'salida', (utcnow() + timedelta(hours=24)).replace(tzinfo=None), 'UTC')
        db.session.commit()

        assert BoardingPassRule().evaluate(_contexto(trip, [tren])) == []

    def test_aprieta_al_acercarse_la_salida(self, app, trip, segment_factory):
        """Online check-in closes hours before the gate does, and after that
        the only remedy is a queue at the airport."""
        from app.utils.timeutil import set_instant

        inminente = segment_factory(numero='IB4444')
        set_instant(inminente, 'salida',
                    (utcnow() + timedelta(hours=3)).replace(tzinfo=None), 'UTC')
        db.session.commit()

        candidatos = BoardingPassRule().evaluate(_contexto(trip, [inminente]))

        assert candidatos[0].severidad is AlertSeverity.ALTA


@pytest.mark.integration
class TestCuandoCalla:
    def test_con_la_tarjeta_adjunta(self, app, trip, gestor, vuelo):
        pase = _documento(trip, gestor, DocumentType.TARJETA_EMBARQUE,
                          texto='TARJETA DE EMBARQUE IB3210 BCN-LHR Asiento 15D')

        assert BoardingPassRule().evaluate(_contexto(trip, [vuelo], [pase])) == []

    def test_una_reserva_no_basta(self, app, trip, gestor, vuelo):
        """The whole point of the new document type: a booking is not proof
        that anybody checked in."""
        reserva = _documento(trip, gestor, DocumentType.RESERVA,
                             texto='Confirmación de reserva IB3210 BCN-LHR')

        assert len(BoardingPassRule().evaluate(_contexto(trip, [vuelo], [reserva]))) == 1

    def test_si_el_segmento_salio_de_esa_tarjeta(self, app, trip, gestor, vuelo):
        """The exact link, for when the pass carries no readable text."""
        pase = _documento(trip, gestor, DocumentType.TARJETA_EMBARQUE, texto='')
        vuelo.documento_origen_id = pase.id
        db.session.commit()

        assert BoardingPassRule().evaluate(_contexto(trip, [vuelo], [pase])) == []


@pytest.mark.security
class TestUnaTarjetaNoRespondePorOtroVuelo:
    """The failure worth catching, because it is the silent one."""

    def test_la_de_ida_no_cubre_la_vuelta(self, app, trip, gestor, vuelo, segment_factory):
        from app.utils.timeutil import set_instant

        vuelta = segment_factory(numero='IB3211', origen='LHR', destino='BCN')
        set_instant(vuelta, 'salida',
                    (utcnow() + timedelta(hours=40)).replace(tzinfo=None), 'UTC')
        db.session.commit()

        pase = _documento(trip, gestor, DocumentType.TARJETA_EMBARQUE,
                          texto='TARJETA DE EMBARQUE IB3210 BCN-LHR')

        candidatos = BoardingPassRule().evaluate(
            _contexto(trip, [vuelo, vuelta], [pase]))

        assert len(candidatos) == 1
        assert 'IB3211' in str(candidatos[0].evidencia)

    def test_la_de_otra_persona_no_cubre_la_mia(
        self, app, trip, gestor, viajero, otro_viajero, segment_factory,
    ):
        from app.services import trip_service
        from app.utils.timeutil import set_instant

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)
        mio = trip.traveler_for(viajero.id)
        suyo = trip.traveler_for(otro_viajero.id)

        vuelo_mio = segment_factory(numero='IB3210', traveler=mio)
        set_instant(vuelo_mio, 'salida',
                    (utcnow() + timedelta(hours=24)).replace(tzinfo=None), 'UTC')
        db.session.commit()

        suyo_pase = _documento(trip, gestor, DocumentType.TARJETA_EMBARQUE,
                               texto='TARJETA DE EMBARQUE IB3210', traveler=suyo)

        candidatos = BoardingPassRule().evaluate(
            _contexto(trip, [vuelo_mio], [suyo_pase], travelers=[mio, suyo]))

        assert len(candidatos) == 1


@pytest.mark.unit
class TestReconocerLaTarjeta:
    """Detection is deliberately strict.

    Failing to recognise one costs an alert nobody needed; recognising a
    booking as one costs the alert that mattered.
    """

    def _es(self, texto, nombre=None):
        from app.services.classification_service import es_tarjeta_de_embarque

        return es_tarjeta_de_embarque(texto, nombre)

    def test_la_frase_basta(self):
        assert self._es('TARJETA DE EMBARQUE\nIB3210 BCN-LHR') is True
        assert self._es('BOARDING PASS\nSeat 15D') is True

    def test_una_reserva_no_lo_es(self):
        assert self._es('Confirmación de reserva\nLocalizador ABC123') is False

    def test_mencionar_la_puerta_de_embarque_no_basta(self):
        """Itineraries say it as advice, and a booking that mentioned it once
        would otherwise silence the alert."""
        assert self._es(
            'Itinerario: acuda a la puerta de embarque con 40 minutos.') is False

    def test_dos_señales_debiles_si(self):
        assert self._es('Gate: 24\nSequence: 031') is True


@pytest.mark.integration
class TestSeReconoceAlProcesar:
    def test_un_documento_de_vuelo_se_reetiqueta(self, app, trip, gestor):
        """Most people upload without changing the default type, and a
        boarding pass filed as «otro» is one nobody can be told is missing."""
        from app.models.enums import DocumentClassification
        from app.tasks.document_tasks import _marcar_si_es_tarjeta_de_embarque

        doc = _documento(trip, gestor, DocumentType.OTRO)
        doc.clasificacion = DocumentClassification.VUELO

        _marcar_si_es_tarjeta_de_embarque(doc, 'TARJETA DE EMBARQUE IB3210')

        assert doc.tipo is DocumentType.TARJETA_EMBARQUE

    def test_no_se_pisa_lo_que_dijo_una_persona(self, app, trip, gestor):
        """Extraction proposes. Overruling somebody who chose «factura»
        because a regex disagreed is the wrong way round."""
        from app.models.enums import DocumentClassification
        from app.tasks.document_tasks import _marcar_si_es_tarjeta_de_embarque

        doc = _documento(trip, gestor, DocumentType.FACTURA)
        doc.clasificacion = DocumentClassification.VUELO

        _marcar_si_es_tarjeta_de_embarque(doc, 'TARJETA DE EMBARQUE IB3210')

        assert doc.tipo is DocumentType.FACTURA


@pytest.mark.integration
class TestEstaAdministrada:
    def test_la_regla_esta_registrada(self, app):
        from app.services.alerts.base import registered_codes

        assert 'sin_tarjeta_de_embarque' in registered_codes()

    def test_su_umbral_se_siembra_en_48_horas(self, app, seeded):
        from app.models.alert import AlertRuleSetting

        fila = AlertRuleSetting.query.filter_by(
            regla='sin_tarjeta_de_embarque').first()

        assert fila is not None
        assert fila.parametros['horas_antes'] == 48

    def test_el_tipo_documental_se_puede_elegir_al_subir(
        self, as_user, gestor, trip,
    ):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/viaje/{trip.id}/subir').get_data(as_text=True)

        assert 'tarjeta_embarque' in html
