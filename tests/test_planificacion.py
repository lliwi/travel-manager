"""Suggesting how to travel, without pretending to know what is bookable.

This application has no availability or pricing connector. A model asked for
«el mejor vuelo» answers with a flight number and a price that came from
nowhere, and somebody acts on it -- which is worse than answering nothing.

What it can do honestly is grounded in what we hold: the catalogue knows the
airports, the countries, their timezones and whether Schengen applies, and the
organisation's own thresholds are in settings. The prompt says which parts are
reference data and which are the model's general knowledge.
"""
import pytest

from app.services import ai_service


@pytest.mark.unit
class TestLoQueSeLeCuentaAlModelo:
    def test_el_catalogo_resuelve_los_lugares(self, app, seeded):
        contexto = ai_service._contexto_de_planificacion(
            'BCN', 'LGW', None, None, 1,
        )

        assert contexto['origen']['pais'] == 'ES'
        assert contexto['destino']['pais'] == 'GB'

    def test_se_dice_si_hay_frontera(self, app, seeded):
        """Whether two places are both in Schengen decides whether a border is
        crossed, and that is not something to take on trust from a model when
        we hold the answer."""
        contexto = ai_service._contexto_de_planificacion(
            'BCN', 'LGW', None, None, 1,
        )

        assert contexto['origen']['schengen'] is True
        assert contexto['destino']['schengen'] is False

    def test_se_dicen_las_zonas_horarias(self, app, seeded):
        contexto = ai_service._contexto_de_planificacion(
            'BCN', 'LGW', None, None, 1,
        )

        assert contexto['origen']['zona_horaria'] == 'Europe/Madrid'
        assert contexto['destino']['zona_horaria'] == 'Europe/London'

    def test_un_lugar_desconocido_se_marca_como_tal(self, app, seeded):
        """So the model can say it does not know rather than assume we do."""
        contexto = ai_service._contexto_de_planificacion(
            'Ciudad Inexistente', 'LGW', None, None, 1,
        )

        assert contexto['origen']['en_catalogo'] is False

    def test_se_incluyen_los_limites_de_la_organizacion(self, app, seeded):
        from app.services import settings_service

        settings_service.set_value('POLITICA_COSTE_MAXIMO_NOCHE', 120)

        contexto = ai_service._contexto_de_planificacion(
            'BCN', 'LGW', None, None, 1,
        )

        assert contexto['politica']['coste_maximo_noche'] == 120

    def test_los_margenes_de_conexion_van_dentro(self, app, seeded):
        contexto = ai_service._contexto_de_planificacion(
            'BCN', 'LGW', None, None, 1,
        )

        assert contexto['margenes_de_conexion_min']['internacional'] == 150


@pytest.mark.security
class TestElPromptProhibeInventar:
    """The instruction that matters most, pinned so it cannot be softened.

    A bookable-looking fact that came from nowhere is the one failure mode that
    makes this feature harmful rather than merely unhelpful.
    """

    def test_prohibe_numeros_de_vuelo(self):
        from app.services.ai.prompts import PROMPTS

        assert 'No inventes números de vuelo' in PROMPTS['plan_trip']

    def test_prohibe_horarios_y_precios(self):
        from app.services.ai.prompts import PROMPTS

        prompt = PROMPTS['plan_trip']

        assert 'No des horarios' in prompt
        assert 'No des precios' in prompt

    def test_el_esquema_no_tiene_donde_poner_un_precio(self):
        """Shape is a stronger instruction than prose: a field that does not
        exist cannot be filled in."""
        import json

        from app.services.ai.schemas import SCHEMAS

        texto = json.dumps(SCHEMAS['plan_trip'])

        assert 'precio' not in texto
        assert 'numero_vuelo' not in texto
        assert 'horario' not in texto

    def test_no_lleva_datos_personales(self, app, seeded, gestor, monkeypatch):
        """A place, two dates and a headcount: nothing about a person."""
        capturado = {}

        def _falso(tarea, request, **kwargs):
            capturado['request'] = request
            from app.services.ai.base import AIResponse

            return AIResponse(contenido='{"opciones": []}', modelo='x'), _Run()

        class _Run:
            id = 'x'
            modelo = 'x'

        monkeypatch.setattr(ai_service, '_run', _falso)

        ai_service.plan_trip(gestor, 'Barcelona', 'Londres')

        assert capturado['request'].contiene_pii is False
        assert capturado['request'].contiene_documentos is False


@pytest.mark.integration
class TestLaPantalla:
    def test_se_ofrece_al_crear_un_viaje(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'Planificar con el asistente' in html

    def test_sin_conector_dice_que_no_es_un_buscador(self, as_user, gestor, seeded):
        """Somebody arriving expecting prices should learn otherwise here,
        not by trusting an answer that looks like one.

        Conditional since the connector exists: the panel used to state flatly
        that the application was not connected to any pricing system, which
        stopped being true the day an administrator connected one.
        """
        with as_user(gestor) as client:
            html = client.get('/trips/planificar').get_data(as_text=True)

        assert 'Hoy no es un buscador' in html
        assert 'qué vuelo sale' in html

    def test_sin_origen_no_se_llama_al_modelo(
        self, as_user, gestor, seeded, monkeypatch
    ):
        llamadas = []
        monkeypatch.setattr(
            ai_service, 'plan_trip', lambda **k: llamadas.append(k) or {},
        )

        with as_user(gestor) as client:
            client.post('/trips/planificar', data={'destino': 'Londres'})

        assert not llamadas

    def test_un_fallo_del_modelo_no_rompe_la_pagina(
        self, as_user, gestor, seeded, monkeypatch
    ):
        from app.utils.errors import TransientError

        def _revienta(**kwargs):
            raise TransientError('el proveedor no respondió')

        monkeypatch.setattr(ai_service, 'plan_trip', _revienta)

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar', data={
                'origen': 'Barcelona', 'destino': 'Londres', 'viajeros': 1,
            })

        assert respuesta.status_code == 200


@pytest.mark.security
class TestQuienPuedePlanificar:
    def test_un_viajero_no_crea_viajes_ni_planifica(self, as_user, viajero, seeded):
        with as_user(viajero) as client:
            respuesta = client.get('/trips/planificar')

        assert respuesta.status_code == 403
