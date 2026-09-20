"""Auto-approval has to be reachable, or the switch is a lie.

The panel offered it, an administrator turned it on, and nothing ever happened:
per-field confidence is 0.85 when a value was copied literally from the document
and 0.50 when it was inferred, so the mean could never reach the 0.95 the
threshold demanded. The control could not act at any setting.

The criterion is now the weakest field, which is both reachable and stricter in
the way that matters: a mean lets one inferred value hide behind several copied
ones, and that value is exactly the one worth reading.
"""
import pytest

from app.extensions import db
from app.services import extraction_service, settings_service


def _payload(confianzas, avisos=None):
    # Every scored field carries a value: an absent field is skipped on
    # purpose, so scoring one that is not there would test nothing.
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
class TestLaConfianzaMinima:
    def test_es_la_del_campo_mas_debil(self, extraccion):
        _preparar(extraccion, {'numero_vuelo': 0.85, 'origen_codigo': 0.5})

        assert extraction_service.confianza_minima(extraccion) == 0.5

    def test_sin_confianzas_no_hay_minimo(self, extraccion):
        _preparar(extraccion, {})

        assert extraction_service.confianza_minima(extraccion) is None


@pytest.mark.unit
class TestCuandoSeApruebaSola:
    def test_desactivada_no_aprueba_aunque_todo_sea_literal(self, app, extraccion):
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', False)
        _preparar(extraccion, {'numero_vuelo': 0.85, 'origen_codigo': 0.85})

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert 'desactivada' in motivo

    def test_todo_literal_se_aprueba(self, app, extraccion):
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        settings_service.set_value('DOCUMENTOS_UMBRAL_AUTO_APROBAR', 0.85)
        _preparar(extraccion, {'numero_vuelo': 0.85, 'origen_codigo': 0.85})

        puede, _ = extraction_service.puede_aprobarse_sola(extraccion)

        assert puede

    def test_un_solo_campo_inferido_lo_impide(self, app, extraccion):
        """The point of using the minimum: it cannot be averaged away.

        Four literal fields and one inferred average 0.78, which a mean-based
        threshold of 0.7 would wave through -- and the inferred one is the
        field a reviewer would have wanted to see.
        """
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        settings_service.set_value('DOCUMENTOS_UMBRAL_AUTO_APROBAR', 0.85)
        _preparar(extraccion, {
            'a': 0.85, 'b': 0.85, 'c': 0.85, 'd': 0.85, 'localizador': 0.5,
        })

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert '0.50' in motivo

    def test_un_aviso_de_normalizacion_lo_impide(self, app, extraccion):
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        settings_service.set_value('DOCUMENTOS_UMBRAL_AUTO_APROBAR', 0.85)
        _preparar(
            extraccion, {'numero_vuelo': 0.85},
            avisos=['La zona horaria no corresponde al aeropuerto.'],
        )

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert 'aviso' in motivo

    def test_sin_confianzas_se_pide_revision(self, app, extraccion):
        """Absence of evidence is a reason to ask, not to assume the best."""
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        _preparar(extraccion, {})

        puede, motivo = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede
        assert 'confianza' in motivo

    def test_un_ano_inventado_lo_impide(self, app, extraccion):
        """Invented-year detection zeroes the field; the minimum sees it."""
        settings_service.set_value('DOCUMENTOS_AUTO_APROBAR', True)
        settings_service.set_value('DOCUMENTOS_UMBRAL_AUTO_APROBAR', 0.85)
        _preparar(extraccion, {'numero_vuelo': 0.85, 'salida': 0.0})

        puede, _ = extraction_service.puede_aprobarse_sola(extraccion)

        assert not puede


@pytest.mark.unit
class TestElUmbralEsAlcanzable:
    def test_el_valor_por_defecto_puede_cumplirse(self):
        """The bug in one assertion: the default demanded more than exists."""
        from app.services.ai_service import CONFIANZA_LITERAL
        from app.services.settings_service import DEFAULTS

        umbral = DEFAULTS['DOCUMENTOS_UMBRAL_AUTO_APROBAR'][0]

        assert umbral <= CONFIANZA_LITERAL, (
            f'Ningún campo supera {CONFIANZA_LITERAL}, así que un umbral de '
            f'{umbral} no se cumple nunca y el interruptor no hace nada.'
        )


@pytest.mark.unit
class TestLoQueNoEstaNoCuenta:
    """A booking never fills every optional field.

    Grounding scores an absent value 0.0 -- honest for display, since nothing
    was found -- but counting it as the weakest field meant every real document
    scored zero, and auto-approval stayed unreachable at any threshold.
    """

    def test_un_campo_vacio_no_baja_la_minima(self, extraccion):
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604', 'asiento': None},
                'confianzas': {'numero_vuelo': 0.85, 'asiento': 0.0},
            }],
            'avisos': [],
        }
        db.session.commit()

        assert extraction_service.confianza_minima(extraccion) == 0.85

    def test_un_campo_con_valor_si_la_baja(self, extraccion):
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604', 'asiento': '14C'},
                'confianzas': {'numero_vuelo': 0.85, 'asiento': 0.5},
            }],
            'avisos': [],
        }
        db.session.commit()

        assert extraction_service.confianza_minima(extraccion) == 0.5
