"""Data extracted from a document reaching the itinerary without waiting.

A deliberate inversion of «a human confirms before data becomes real»: with the
setting on, the data lands and the correction happens afterwards. What the
confidence buys is no longer a gate but a mark -- what is doubtful arrives
flagged rather than withheld -- and the review screen stays reachable so the
correction has somewhere to happen.

The cost is stated rather than hidden: a value the model invented now reaches
the itinerary. It arrives flagged, attributable to its document and
correctable, which is the bargain an organisation makes by turning this on.
"""
import pytest

from app.extensions import db
from app.services import extraction_service, settings_service


def _payload(confianzas, avisos=None):
    return {
        'servicios': [{
            'campos': {nombre: f'valor-{nombre}' for nombre in confianzas},
            'confianzas': confianzas,
        }],
        'avisos': avisos or [],
    }


@pytest.fixture
def extraccion(documento_procesado):
    return documento_procesado.current_extraction


def _preparar(extraccion, confianzas, avisos=None):
    extraccion.payload_normalizado = _payload(confianzas, avisos)
    extraccion.avisos = avisos or []
    db.session.commit()
    return extraccion


@pytest.mark.unit
class TestCuandoSeAplicaSola:
    def test_desactivada_no_aplica_nada(self, app, extraccion):
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', False)
        _preparar(extraccion, {'numero_vuelo': 0.85})

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert 'desactivada' in motivo

    def test_activada_aplica_aunque_la_confianza_sea_baja(self, app, extraccion):
        """The point of the change: importing is what triggers it, not a score."""
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        _preparar(extraccion, {'numero_vuelo': 0.5, 'localizador': 0.5})

        puede, _ = extraction_service.puede_aprobarse_sola(extraccion)

        assert puede

    def test_activada_aplica_aunque_haya_avisos(self, app, extraccion):
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        _preparar(
            extraccion, {'numero_vuelo': 0.85},
            avisos=['La zona horaria no corresponde al aeropuerto.'],
        )

        puede, _ = extraction_service.puede_aprobarse_sola(extraccion)

        assert puede

    def test_no_se_aplica_dos_veces(self, app, extraccion):
        from app.models.enums import ExtractionState

        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        _preparar(extraccion, {'numero_vuelo': 0.85})
        extraccion.estado = ExtractionState.APROBADA
        db.session.commit()

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert 'ya estaba aprobada' in motivo

    def test_el_motivo_dice_con_que_confianza_entro(self, app, extraccion):
        """So the log says what was applied, not just that something was."""
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        _preparar(extraccion, {'numero_vuelo': 0.85, 'localizador': 0.5})

        _, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert '0.50' in motivo


@pytest.mark.unit
class TestLoDudosoLlegaMarcado:
    """Withheld before, flagged now. The confidence still does something."""

    def test_un_campo_por_debajo_del_umbral_marca_la_entidad(
        self, gestor, documento_procesado
    ):
        from app.models.enums import DocumentClassification

        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraccion = documento_procesado.current_extraction
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604', 'localizador': 'ODIVYR'},
                'confianzas': {'numero_vuelo': 0.85, 'localizador': 0.5},
            }],
            'avisos': [],
        }
        extraccion.payload = extraccion.payload_normalizado
        db.session.commit()

        resultado = extraction_service.approve(gestor, extraccion)

        entidad = resultado['entities'][0]
        assert entidad.requiere_revision, (
            'Si el dato entra sin que nadie lo mire, el itinerario tiene que '
            'decir cuál mirar.'
        )


@pytest.mark.unit
class TestLaConfianzaMinima:
    def test_es_la_del_campo_mas_debil(self, extraccion):
        _preparar(extraccion, {'numero_vuelo': 0.85, 'origen_codigo': 0.5})

        assert extraction_service.confianza_minima(extraccion) == 0.5

    def test_un_campo_vacio_no_cuenta(self, extraccion):
        """A field the document never mentions is absent, not unreliable."""
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604', 'asiento': None},
                'confianzas': {'numero_vuelo': 0.85, 'asiento': 0.0},
            }],
            'avisos': [],
        }
        db.session.commit()

        assert extraction_service.confianza_minima(extraccion) == 0.85

    def test_sin_confianzas_no_hay_minimo(self, extraccion):
        _preparar(extraccion, {})

        assert extraction_service.confianza_minima(extraccion) is None


@pytest.mark.unit
class TestCorregirDespuesDeAplicado:
    """The review screen has to remain useful, or the bargain does not hold.

    Applying without asking is only acceptable while the correction is still
    possible; ``approve`` refused an already-approved extraction outright,
    which closed the one door the whole arrangement depends on.
    """

    def _aprobada(self, gestor, documento_procesado):
        from app.models.enums import DocumentClassification

        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraccion = documento_procesado.current_extraction
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604', 'origen_codigo': 'BCN'},
                'confianzas': {'numero_vuelo': 0.85},
            }],
            'avisos': [],
        }
        extraccion.payload = extraccion.payload_normalizado
        db.session.commit()
        extraction_service.approve(gestor, extraccion)
        return extraccion

    def test_una_correccion_posterior_se_aplica(self, gestor, documento_procesado):
        from app.models.itinerary import TravelSegment

        self._aprobada(gestor, documento_procesado)
        extraccion = documento_procesado.current_extraction

        extraction_service.approve(
            gestor, extraccion, correcciones={0: {'numero_vuelo': 'VY9999'}},
        )

        assert TravelSegment.query.filter_by(numero='VY9999').first() is not None

    def test_volver_a_aplicar_sin_cambios_se_rechaza(
        self, gestor, documento_procesado
    ):
        """Nothing to say is not a correction."""
        from app.utils.errors import ConflictError

        self._aprobada(gestor, documento_procesado)
        extraccion = documento_procesado.current_extraction

        with pytest.raises(ConflictError, match='ya se ha aplicado'):
            extraction_service.approve(gestor, extraccion)

    def test_la_correccion_no_duplica_la_entidad(self, gestor, documento_procesado):
        from app.models.itinerary import TravelSegment

        self._aprobada(gestor, documento_procesado)
        extraccion = documento_procesado.current_extraction
        antes = TravelSegment.query.filter_by(
            trip_id=documento_procesado.trip_id, is_deleted=False,
        ).count()

        extraction_service.approve(
            gestor, extraccion, correcciones={0: {'numero_vuelo': 'VY9999'}},
        )

        despues = TravelSegment.query.filter_by(
            trip_id=documento_procesado.trip_id, is_deleted=False,
        ).count()
        assert despues == antes, 'Corregir actualiza el tramo, no crea otro.'
