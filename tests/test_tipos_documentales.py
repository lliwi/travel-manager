"""Every field a document type declares has somewhere to land.

A visa extraction read eight fields from the document and wrote none of them:
the classification existed, the schema existed, the model filled it in, the
manager approved -- and the mapping to storage had no entry for a single one.
The expiry date went with the rest, which is the one thing a visa is checked
for.
"""
import pytest

from app.extensions import db
from app.models.enums import DocumentClassification
from app.services import extraction_service
from app.services.ai.schemas import SCHEMAS, campos_de


def _destinos(kind):
    return (
        set(extraction_service.FIELD_MAPPINGS.get(kind, {}))
        | set(extraction_service.INSTANT_MAPPINGS.get(kind, {}))
        | set(extraction_service.EXTRA_FIELDS.get(kind, ()))
        | set(extraction_service.DERIVED_FIELDS.get(kind, ()))
    )


@pytest.mark.unit
class TestNingunCampoSePierde:
    """The barrier: a schema field with no destination is data thrown away."""

    @pytest.mark.parametrize('clase,kind', [
        ('vuelo', 'segmento'),
        ('tren', 'segmento'),
        ('hotel', 'alojamiento'),
        ('vehiculo', 'vehiculo'),
        ('seguro', 'servicio'),
        ('visado', 'servicio'),
        ('otro', 'servicio'),
    ])
    def test_todo_campo_extraido_tiene_destino(self, clase, kind):
        campos = set(campos_de(SCHEMAS[f'extract_{clase}']) or {})

        perdidos = sorted(campos - _destinos(kind))

        assert not perdidos, (
            f'«{clase}» extrae {perdidos} y no hay dónde guardarlo: el modelo '
            f'lo lee, el gestor lo aprueba y se tira.'
        )


@pytest.mark.unit
class TestUnVisadoSeGuardaEntero:
    def _aprobar(self, gestor, documento_procesado, campos):
        documento_procesado.clasificacion = DocumentClassification.VISADO
        extraction = documento_procesado.current_extraction
        payload = {'servicios': [{'campos': campos, 'confianzas': {}}]}
        extraction.payload = payload
        extraction.payload_normalizado = payload
        db.session.commit()
        return extraction_service.approve(gestor, extraction)['entities'][0]

    def _visado(self):
        return {
            'numero': 'ETA-9988',
            'tipo': 'Autorización electrónica',
            'pais_emisor': 'GB',
            'titular': 'Llibert Morreres',
            'entradas': 'Múltiples',
            'fecha_emision': {'local': '2026-09-01T00:00', 'zona_horaria': 'Europe/London'},
            'fecha_caducidad': {'local': '2028-09-01T00:00', 'zona_horaria': 'Europe/London'},
            'estancia_maxima_dias': 180,
        }

    def test_el_numero_y_el_pais_van_a_sus_columnas(self, gestor, documento_procesado):
        entidad = self._aprobar(gestor, documento_procesado, self._visado())

        assert entidad.localizador == 'ETA-9988'
        assert entidad.pais == 'GB'

    def test_la_caducidad_queda_como_instante(self, gestor, documento_procesado):
        """A date the expiry rule can compare, not a string it cannot."""
        from datetime import datetime

        entidad = self._aprobar(gestor, documento_procesado, self._visado())

        assert entidad.fin_local == datetime(2028, 9, 1, 0, 0)
        assert entidad.fin_tz == 'Europe/London'
        assert entidad.fin_utc is not None

    def test_lo_demas_se_guarda_en_los_datos(self, gestor, documento_procesado):
        entidad = self._aprobar(gestor, documento_procesado, self._visado())

        assert entidad.datos['titular'] == 'Llibert Morreres'
        assert entidad.datos['entradas'] == 'Múltiples'
        assert entidad.datos['estancia_maxima_dias'] == 180

    def test_queda_procedencia_de_lo_guardado_aparte(
        self, gestor, documento_procesado
    ):
        """Data in a JSON column is still data, and still has to be traceable."""
        from app.models.itinerary import FieldProvenance

        entidad = self._aprobar(gestor, documento_procesado, self._visado())

        campos = {
            p.campo for p in FieldProvenance.query.filter_by(
                entidad_id=entidad.id,
            ).all()
        }
        assert 'titular' in campos


@pytest.mark.unit
class TestUnSeguroTambien:
    def test_la_cobertura_y_el_telefono_se_guardan(self, gestor, documento_procesado):
        documento_procesado.clasificacion = DocumentClassification.SEGURO
        extraction = documento_procesado.current_extraction
        payload = {'servicios': [{'campos': {
            'aseguradora': 'Mapfre',
            'numero_poliza': 'POL-1234',
            'asegurado': 'Llibert Morreres',
            'cobertura': 'Asistencia en viaje hasta 30.000 EUR',
            'telefono_asistencia': '+34 900 000 000',
        }, 'confianzas': {}}]}
        extraction.payload = payload
        extraction.payload_normalizado = payload
        db.session.commit()

        entidad = extraction_service.approve(gestor, extraction)['entities'][0]

        assert entidad.proveedor == 'Mapfre'
        assert entidad.localizador == 'POL-1234'
        assert entidad.datos['cobertura'].startswith('Asistencia')
        assert entidad.datos['telefono_asistencia'] == '+34 900 000 000'


@pytest.mark.unit
class TestLoQueSeTiraAProposito:
    """«No lo guardamos» y «nadie se dio cuenta de que se perdía» no son lo mismo."""

    def test_las_noches_se_calculan_no_se_guardan(self, gestor, documento_procesado):
        """Keeping the model's count would let it disagree with the dates.

        The dates are the evidence; a number beside them that says something
        else is a second opinion nobody asked for.
        """
        from datetime import datetime

        from app.models.enums import DocumentClassification

        documento_procesado.clasificacion = DocumentClassification.HOTEL
        extraction = documento_procesado.current_extraction
        payload = {'servicios': [{'campos': {
            'nombre': 'Hotel',
            'noches': 99,
            'check_in': {'local': '2026-10-31T15:00', 'zona_horaria': 'Europe/London'},
            'check_out': {'local': '2026-11-02T10:00', 'zona_horaria': 'Europe/London'},
        }, 'confianzas': {}}]}
        extraction.payload = payload
        extraction.payload_normalizado = payload
        db.session.commit()

        entidad = extraction_service.approve(gestor, extraction)['entities'][0]

        assert entidad.noches == 2, 'Las noches salen de las fechas.'
        assert 'noches' not in (entidad.datos or {})
        assert entidad.check_in_local == datetime(2026, 10, 31, 15, 0)


@pytest.mark.unit
class TestElUmbralSaleDeLaImagen:
    """A fixed cut at 160 assumes a clean scan on white paper.

    A photographed ticket, a grey fax or a dark-mode boarding pass falls
    entirely on one side of it and comes back blank. Otsu derives the cut from
    the image instead of assuming one.
    """

    def _histograma(self, oscuros, claros, nivel_oscuro=40, nivel_claro=210):
        h = [0] * 256
        h[nivel_oscuro] = oscuros
        h[nivel_claro] = claros
        return h

    def test_separa_tinta_de_papel(self):
        from app.services.ocr_service import _umbral_otsu

        umbral = _umbral_otsu(self._histograma(oscuros=1000, claros=9000))

        assert 40 <= umbral < 210

    def test_una_pagina_oscura_no_se_queda_en_blanco(self):
        """Both levels below the old fixed 160: everything was «paper»."""
        from app.services.ocr_service import _umbral_otsu

        umbral = _umbral_otsu(
            self._histograma(oscuros=2000, claros=8000,
                             nivel_oscuro=10, nivel_claro=120)
        )

        assert umbral < 160, 'El corte debe caer entre los dos, no por encima.'
        assert 10 <= umbral < 120

    def test_una_imagen_vacia_no_rompe(self):
        from app.services.ocr_service import _umbral_otsu

        assert _umbral_otsu([0] * 256) == 128


@pytest.mark.unit
class TestSeEligeLaMejorLectura:
    """Tesseract answers something for every mode; the longest is not the best.

    Reading a ticket as one paragraph interleaves its columns into nonsense,
    and the mode that turned the border of the scan into punctuation would win
    on length alone.
    """

    def test_gana_el_texto_con_palabras(self):
        from app.services.ocr_service import _puntuar_lectura

        bueno = 'Vuelo VY7604 Barcelona Londres salida 07:55'
        ruido = '|| |. ,,, ~~ || .. ,, || ~~ .. || ,, ~~ || .. ,, ~~ ||'

        assert _puntuar_lectura(bueno) > _puntuar_lectura(ruido)

    def test_una_lectura_vacia_no_puntua(self):
        from app.services.ocr_service import _puntuar_lectura

        assert _puntuar_lectura('') == 0
        assert _puntuar_lectura('   ') == 0

    def test_las_letras_sueltas_no_cuentan_como_palabras(self):
        from app.services.ocr_service import _puntuar_lectura

        assert _puntuar_lectura('a b c d e f g') == 0

    def test_se_prueban_varios_modos(self):
        """One uniform block is the wrong assumption for a boarding pass."""
        from app.services.ocr_service import _MODOS_SEGMENTACION

        assert len(_MODOS_SEGMENTACION) > 1
        assert 6 in _MODOS_SEGMENTACION
