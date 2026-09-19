"""The assistant panel: one question, one answer, nothing kept.

Deliberately not a chat. There is no thread, so there is no earlier answer to
contaminate the next question and no history to carry data between askers. What
these tests pin is the boundary: the panel reaches the same service ``/api/v1``
does, it refuses whoever may not consult it, and a traveller cannot ask their
way into another traveller's itinerary.
"""
import pytest

from app.extensions import db


def _url(trip):
    return f'/trips/{trip.id}/asistente'


@pytest.mark.integration
class TestPreguntaYRespuesta:
    def test_el_panel_se_abre_vacio(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(_url(trip)).get_data(as_text=True)

        assert 'Asistente' in html
        assert 'Respuesta' not in html, 'Sin preguntar no hay respuesta que mostrar.'

    def test_se_responde_a_una_pregunta(self, as_user, gestor, trip, segment_factory):
        segment_factory(numero='IB3210')
        db.session.commit()

        with as_user(gestor) as client:
            html = client.post(_url(trip), data={
                'pregunta': '¿A qué hora salgo?',
            }).get_data(as_text=True)

        assert 'Respuesta' in html

    def test_una_pregunta_vacia_no_llega_al_modelo(
        self, as_user, gestor, trip, monkeypatch
    ):
        """Validation is free; a round trip to the model is not."""
        from app.services import ai_service

        llamadas = []
        monkeypatch.setattr(
            ai_service, 'answer_trip_question',
            lambda **kw: llamadas.append(kw) or {'respuesta': 'x'},
        )

        with as_user(gestor) as client:
            html = client.post(_url(trip), data={'pregunta': '   '}).get_data(as_text=True)

        assert not llamadas
        assert 'Escriba la pregunta' in html

    def test_se_pasa_la_pregunta_ya_recortada(
        self, as_user, gestor, trip, monkeypatch
    ):
        from app.services import ai_service

        recibido = {}

        def _fake(**kwargs):
            recibido.update(kwargs)
            return {'respuesta': 'Sale a las 07:55.', 'referencias': [],
                    'confianza': 0.9, 'datos_insuficientes': False,
                    'fecha_datos': '2026-09-19', 'run_id': 'x',
                    'proveedor': 'stub', 'modelo': 'stub'}

        monkeypatch.setattr(ai_service, 'answer_trip_question', _fake)

        with as_user(gestor) as client:
            html = client.post(_url(trip), data={
                'pregunta': '  ¿A qué hora salgo?  ',
            }).get_data(as_text=True)

        assert recibido['pregunta'] == '¿A qué hora salgo?'
        assert recibido['trip'].id == trip.id
        assert 'Sale a las 07:55.' in html

    def test_se_avisa_cuando_faltan_datos(self, as_user, gestor, trip, monkeypatch):
        """An answer built on nothing must not look like an answer."""
        from app.services import ai_service

        monkeypatch.setattr(ai_service, 'answer_trip_question', lambda **kw: {
            'respuesta': 'No consta.', 'referencias': [], 'confianza': 0.1,
            'datos_insuficientes': True, 'fecha_datos': '2026-09-19',
            'run_id': 'x', 'proveedor': 'stub', 'modelo': 'stub',
        })

        with as_user(gestor) as client:
            html = client.post(_url(trip), data={
                'pregunta': '¿Cuánto cuesta el hotel?',
            }).get_data(as_text=True)

        assert 'datos suficientes' in html

    def test_un_fallo_del_modelo_no_rompe_la_pagina(
        self, as_user, gestor, trip, monkeypatch
    ):
        """Ollama being down is an ordinary Tuesday, not a 500."""
        from app.services import ai_service
        from app.utils.errors import TransientError

        def _boom(**kwargs):
            raise TransientError('Ollama no respondió en 120 s.')

        monkeypatch.setattr(ai_service, 'answer_trip_question', _boom)

        with as_user(gestor) as client:
            respuesta = client.post(_url(trip), data={'pregunta': '¿Y bien?'})

        assert respuesta.status_code == 200
        assert 'Respuesta' not in respuesta.get_data(as_text=True)


@pytest.mark.security
class TestQuienPuedePreguntar:
    def test_un_ajeno_al_viaje_no_sabe_que_existe(self, as_user, ajeno, trip):
        with as_user(ajeno) as client:
            assert client.get(_url(trip)).status_code == 404

    def test_el_asistente_solo_ve_lo_que_ve_quien_pregunta(
        self, as_user, gestor, trip, viajero, otro_viajero, segment_factory,
        monkeypatch,
    ):
        """Section 5.4, through the web surface this time.

        The isolation is structural: the context is built by the same scoped
        queries the timeline uses, so another traveller's segment is not in the
        payload the model receives -- it cannot leak what it never got.
        """
        from app.services import ai_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)
        mio = trip.traveler_for(viajero.id)
        suyo = trip.traveler_for(otro_viajero.id)
        segment_factory(numero='IB1111', traveler=mio)
        segment_factory(numero='JL9999', traveler=suyo)
        db.session.commit()

        visto = {}
        original = ai_service.build_ai_context

        def _espia(actor, trip_arg):
            contexto = original(actor, trip_arg)
            visto['payload'] = str(contexto['datos'])
            return contexto

        monkeypatch.setattr(ai_service, 'build_ai_context', _espia)

        with as_user(viajero) as client:
            client.post(_url(trip), data={'pregunta': '¿Qué vuelos hay?'})

        assert 'IB1111' in visto['payload']
        assert 'JL9999' not in visto['payload'], (
            'El asistente no puede recibir el tramo de otra persona viajera.'
        )


@pytest.mark.integration
class TestExplicarUnaAlerta:
    def _alerta(self, trip, gestor):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity, AlertState

        alerta = Alert(
            trip_id=trip.id, tipo='conexion', regla='Margen de conexión',
            dedup_key='x' * 16, severidad=AlertSeverity.ALTA,
            estado=AlertState.ABIERTA, titulo='Margen insuficiente',
            mensaje='20 minutos entre dos vuelos.', evidencia={'margen': 20},
        )
        db.session.add(alerta)
        db.session.commit()
        return alerta

    def test_el_boton_aparece_en_el_detalle(self, as_user, gestor, trip):
        alerta = self._alerta(trip, gestor)

        with as_user(gestor) as client:
            html = client.get(f'/alerts/{alerta.id}').get_data(as_text=True)

        assert 'Explicar esta alerta' in html

    def test_se_explica_la_alerta(self, as_user, gestor, trip, monkeypatch):
        from app.services import ai_service

        alerta = self._alerta(trip, gestor)
        monkeypatch.setattr(ai_service, 'explain_alert', lambda actor, a: {
            'explicacion': 'Veinte minutos no bastan para cambiar de terminal.',
            'consecuencias': 'Riesgo de perder el segundo vuelo.',
            'acciones': [
                {'accion': 'Adelantar el primer vuelo', 'prioridad': 'alta'},
                {'accion': 'Reservar una noche en el aeropuerto', 'prioridad': 'baja'},
            ],
            'run_id': 'x',
        })

        with as_user(gestor) as client:
            html = client.post(
                f'/alerts/{alerta.id}/explicar', follow_redirects=True
            ).get_data(as_text=True)

        assert 'Veinte minutos no bastan' in html
        assert 'Adelantar el primer vuelo' in html
        assert 'alta' in html
        assert '{' not in html.split('Acciones posibles')[1][:400], (
            'Una acción es un objeto con prioridad; volcar el diccionario en '
            'pantalla es lo que hacía la plantilla.'
        )

    def test_un_fallo_del_modelo_deja_la_alerta_visible(
        self, as_user, gestor, trip, monkeypatch
    ):
        from app.services import ai_service
        from app.utils.errors import AIError

        alerta = self._alerta(trip, gestor)

        def _boom(actor, a):
            raise AIError('No hay proveedor configurado.')

        monkeypatch.setattr(ai_service, 'explain_alert', _boom)

        with as_user(gestor) as client:
            respuesta = client.post(f'/alerts/{alerta.id}/explicar')

        assert respuesta.status_code == 200
        assert 'Margen insuficiente' in respuesta.get_data(as_text=True)
