"""Volver a leer un documento que salió bien.

«Reintentar» y «Volver a analizar» son dos peticiones distintas por la misma
puerta: la primera reanuda un documento que se rompió desde donde llegó; la
segunda rebobina hasta la clasificación y lo lee otra vez. La segunda es la que
se quiere tras cambiar de modelo, o cuando la extracción se dejó un campo que
sí está en el documento -- un importe, por ejemplo.

Lo que ninguna de las dos puede hacer es tocar el itinerario. La extracción
propone y un gestor aprueba, así que reanalizar deja una versión nueva
esperando revisión y lo aprobado antes sigue exactamente igual.
"""
import pytest

from app.models.enums import DocumentProcessState as S
from app.services import document_service

pytestmark = pytest.mark.integration


class TestCuandoSeOfrece:
    @pytest.mark.parametrize('estado', [
        S.CLASIFICADO, S.EXTRAIDO, S.NORMALIZADO,
        S.PENDIENTE_REVISION, S.REVISADO, S.APROBADO,
    ])
    def test_en_los_estados_que_ya_pasaron_por_clasificacion(
        self, app, documento_procesado, estado,
    ):
        documento_procesado.estado_proceso = estado

        assert documento_procesado.puede_reanalizarse is True

    @pytest.mark.parametrize('estado', [S.RECIBIDO, S.VALIDADO, S.ESCANEADO,
                                        S.ALMACENADO, S.TEXTO_EXTRAIDO])
    def test_no_antes_de_haber_clasificado(self, app, documento_procesado, estado):
        """Rebobinar más atrás lo manda a una tarea que solo puede fallar."""
        documento_procesado.estado_proceso = estado

        assert documento_procesado.puede_reanalizarse is False

    @pytest.mark.parametrize('estado', [S.INFECTADO, S.RECHAZADO, S.DESCARTADO])
    def test_ni_en_un_estado_terminal(self, app, documento_procesado, estado):
        """Ofrecerlo prometería algo que no puede pasar."""
        documento_procesado.estado_proceso = estado

        assert documento_procesado.puede_reanalizarse is False

    def test_en_error_se_ofrece_reintentar_y_no_esto(self, as_user, gestor,
                                                     documento_procesado):
        """Son dos cosas distintas y la pantalla no puede ofrecer las dos."""
        from app.extensions import db

        documento_procesado.estado_proceso = S.ERROR
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(
                f'/documents/{documento_procesado.id}').get_data(as_text=True)

        assert 'Reintentar' in html
        assert 'Volver a analizar' not in html


class TestQueHaceElBoton:
    def test_sale_en_la_pantalla_de_un_documento_aprobado(
        self, as_user, gestor, documento_aprobado,
    ):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/{documento_aprobado.id}').get_data(as_text=True)

        assert 'Volver a analizar' in html
        assert 'desde_el_principio' in html

    def _reprocesar(self, as_user, gestor, documento, monkeypatch, **extra):
        """Lanza el reproceso y devuelve desde qué tarea se encoló."""
        llamadas = []
        monkeypatch.setattr(
            document_service, '_enqueue_pipeline',
            lambda doc, start_from=None: llamadas.append(start_from),
        )

        with as_user(gestor) as client:
            client.post(f'/documents/{documento.id}/reprocesar',
                        data={'csrf_token': 'x', **extra}, follow_redirects=True)

        assert llamadas, 'no se encoló nada'
        return llamadas[-1]

    def test_rebobina_y_vuelve_a_leerlo(self, as_user, gestor,
                                        documento_aprobado, monkeypatch):
        """Rebobina todo lo que el fichero guardado permite -- lo que incluye
        rehacer el texto y la clasificación, no solo la extracción -- y de ahí
        sale una versión nueva que revisar."""
        antes = documento_aprobado.estado_proceso

        desde = self._reprocesar(as_user, gestor, documento_aprobado, monkeypatch,
                                 desde_el_principio='1')

        assert documento_aprobado.estado_proceso is not antes
        # Cualquiera de estas acaba en una extracción nueva; cuál, depende de
        # hasta dónde se pueda rebobinar con lo que queda en almacenamiento.
        assert desde in ('extract_text', 'classify', 'ai_extract')

    def test_va_mas_atras_que_un_simple_reintento(self, app, documento_aprobado):
        """Son dos cosas distintas y tienen que rebobinar distinto: si
        «Reintentar» empezara a rebobinar igual, costaría una llamada al
        modelo cada vez que alguien reintenta un fallo de almacenamiento."""
        from app.models.enums import DocumentProcessState as Estado

        orden = list(Estado)
        reanalizar = document_service._earliest_replayable_state(documento_aprobado)
        reintentar = document_service._last_successful_state(documento_aprobado)

        assert orden.index(reanalizar) <= orden.index(reintentar)

    def test_lo_aprobado_no_se_toca(self, as_user, gestor, documento_aprobado,
                                    monkeypatch):
        """Lo que ya está en el itinerario sigue ahí: la extracción propone y
        alguien aprueba, y reanalizar solo vuelve a proponer."""
        from app.models.itinerary import Accommodation, TravelSegment

        antes = (TravelSegment.query.count(), Accommodation.query.count())
        monkeypatch.setattr(document_service, '_enqueue_pipeline',
                            lambda doc, start_from=None: None)

        with as_user(gestor) as client:
            client.post(f'/documents/{documento_aprobado.id}/reprocesar',
                        data={'csrf_token': 'x', 'desde_el_principio': '1'},
                        follow_redirects=True)

        assert (TravelSegment.query.count(), Accommodation.query.count()) == antes

    def test_queda_en_la_auditoria(self, as_user, gestor, documento_aprobado,
                                   monkeypatch):
        """Cuesta una llamada al modelo, así que consta quién la pidió."""
        from app.models.audit import AuditEvent

        monkeypatch.setattr(document_service, '_enqueue_pipeline',
                            lambda doc, start_from=None: None)
        antes = AuditEvent.query.filter_by(
            accion='document.reprocess_requested').count()

        with as_user(gestor) as client:
            client.post(f'/documents/{documento_aprobado.id}/reprocesar',
                        data={'csrf_token': 'x', 'desde_el_principio': '1'},
                        follow_redirects=True)

        assert AuditEvent.query.filter_by(
            accion='document.reprocess_requested').count() == antes + 1


class TestLosImportesLleganDesdeElDocumento:
    def test_todos_los_esquemas_de_extraccion_los_piden(self, app):
        """Lo comprobado en datos reales: el hotel trajo 327,03 € de su
        reserva. Hay un esquema por tipo de documento, así que olvidar el campo
        en uno es fácil y no lo delataría ningún error -- simplemente ese tipo
        dejaría de traer importes.
        """
        import json

        from app.services.ai.schemas import SCHEMAS

        sin_importe = [
            nombre for nombre, esquema in SCHEMAS.items()
            if nombre.startswith('extract_') and nombre != 'extract_desconocido'
            and '"importe"' not in json.dumps(esquema)
        ]

        assert not sin_importe, f'Esquemas que no piden importe: {sin_importe}'
