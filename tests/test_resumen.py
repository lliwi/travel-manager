"""The executive summary, reachable from the interface.

It had been built, tested and exposed at ``/api/v1`` since M10, and no button
in the application ever called it -- which in practice means it did not exist.
These tests are mostly about the route being wired up and staying wired up.
"""
import pytest


def _url(trip):
    return f'/trips/{trip.id}/resumen'


@pytest.mark.integration
class TestSeLlegaDesdeElViaje:
    def test_el_boton_esta_en_el_detalle(self, as_user, gestor, trip):
        """The part that was missing. Without it the feature is unreachable."""
        with as_user(gestor) as client:
            html = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        assert _url(trip) in html

    def test_la_pagina_se_abre_sin_generar_nada(self, as_user, gestor, trip):
        """A model call on every page view would make the trip page slow for
        everyone, to serve the few who want a summary."""
        with as_user(gestor) as client:
            respuesta = client.get(_url(trip))

        assert respuesta.status_code == 200
        assert 'Generar resumen' in respuesta.get_data(as_text=True)

    def test_se_redacta_al_pedirlo(self, as_user, gestor, trip, segment_factory):
        segment_factory()

        with as_user(gestor) as client:
            html = client.post(_url(trip)).get_data(as_text=True)

        assert 'Resumen ejecutivo' in html


@pytest.mark.integration
class TestLoQueSeVe:
    def test_se_muestran_los_puntos_de_atencion(
        self, as_user, gestor, trip, monkeypatch,
    ):
        """What a manager should check is the reason to read the summary."""
        from app.services import ai_service

        monkeypatch.setattr(ai_service, 'summarize_trip', lambda **_: {
            'resumen': 'Viaje de dos días a Londres.',
            'itinerario': [],
            'puntos_atencion': ['Falta el alojamiento de la noche del día 2.'],
            'fecha_datos': None,
            'run_id': 'x',
        })

        with as_user(gestor) as client:
            html = client.post(_url(trip)).get_data(as_text=True)

        assert 'Falta el alojamiento' in html

    def test_una_hora_nunca_aparece_sin_su_zona(
        self, as_user, gestor, trip, monkeypatch,
    ):
        """An itinerary crosses time zones; an hour on its own is ambiguous."""
        from app.services import ai_service

        monkeypatch.setattr(ai_service, 'summarize_trip', lambda **_: {
            'resumen': 'Resumen.',
            'itinerario': [{
                'fecha': '2026-10-01', 'hora': '08:40',
                'zona_horaria': 'Europe/Madrid',
                'descripcion': 'Salida desde Barcelona.',
            }],
            'puntos_atencion': [],
            'fecha_datos': None,
            'run_id': 'x',
        })

        with as_user(gestor) as client:
            html = client.post(_url(trip)).get_data(as_text=True)

        assert '08:40' in html
        assert 'Europe/Madrid' in html

    def test_un_fallo_del_modelo_no_rompe_la_pagina(
        self, as_user, gestor, trip, monkeypatch,
    ):
        from app.services import ai_service

        def _explota(**_):
            raise RuntimeError('el proveedor no responde')

        monkeypatch.setattr(ai_service, 'summarize_trip', _explota)

        with as_user(gestor) as client:
            respuesta = client.post(_url(trip))

        assert respuesta.status_code == 200
        assert 'No se pudo generar el resumen' in respuesta.get_data(as_text=True)


@pytest.mark.security
class TestQuienPuedeResumir:
    def test_un_ajeno_al_viaje_no_sabe_que_existe(self, as_user, ajeno, trip):
        with as_user(ajeno) as client:
            assert client.get(_url(trip)).status_code == 404

    def test_el_resumen_solo_cubre_lo_que_ve_quien_lo_pide(
        self, as_user, gestor, trip, viajero, otro_viajero, segment_factory,
        monkeypatch,
    ):
        """Section 5.4 again, on this surface.

        The summary reads the trip through ``build_ai_context``, the same
        scoped queries the timeline uses, so another traveller's segment never
        reaches the model -- it cannot summarise what it was never given.
        """
        from app.extensions import db
        from app.services import ai_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)
        segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id))
        segment_factory(numero='JL9999', traveler=trip.traveler_for(otro_viajero.id))
        db.session.commit()

        visto = {}
        original = ai_service.build_ai_context

        def _espia(actor, trip_arg):
            contexto = original(actor, trip_arg)
            visto['payload'] = str(contexto['datos'])
            return contexto

        monkeypatch.setattr(ai_service, 'build_ai_context', _espia)

        with as_user(viajero) as client:
            client.post(_url(trip))

        assert 'IB1111' in visto['payload']
        assert 'JL9999' not in visto['payload'], (
            'El resumen no puede recibir el tramo de otra persona viajera.'
        )
