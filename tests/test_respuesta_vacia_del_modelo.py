"""When the model answers nothing, say why in terms of what to change.

«El modelo no devolvió contenido» names a symptom and no cause, and the cause
was in the response all along: the planner sends a long prompt -- it carries
real flights and hotels now -- a reasoning model spends the whole output budget
thinking, and the answer is cut off before its first character. Nothing looks
broken and no setting looks wrong; it just stops working as the prompt grows.
"""
import pytest

from app.utils.errors import AIError

pytestmark = pytest.mark.unit


class TestElProveedorCompatibleConOpenAI:
    def _vacia(self, finish_reason=None, usage=None):
        from app.services.ai.openai_compatible import _explicar_respuesta_vacia

        with pytest.raises(AIError) as exc:
            _explicar_respuesta_vacia(
                'openai', 'gpt-5.6-luna',
                {'finish_reason': finish_reason, 'message': {'content': ''}},
                usage or {},
            )
        return exc.value.mensaje

    def test_se_nombra_el_ajuste_que_hay_que_subir(self):
        mensaje = self._vacia('length', {'completion_tokens': 2048})

        assert 'Máximo de tokens' in mensaje
        assert '2048' in mensaje

    def test_se_dice_cuanto_se_fue_en_razonar(self):
        """Es el dato que convierte «no respondió» en «no le dio tiempo»."""
        mensaje = self._vacia('length', {
            'completion_tokens': 2048,
            'completion_tokens_details': {'reasoning_tokens': 2048},
        })

        assert 'razonando' in mensaje

    def test_un_filtro_de_contenido_se_distingue(self):
        """Subir el límite no arregla esto, así que no se sugiere."""
        mensaje = self._vacia('content_filter')

        assert 'filtro de contenido' in mensaje
        assert 'Máximo de tokens' not in mensaje

    def test_sin_motivo_conocido_al_menos_se_dice_cual_era(self):
        mensaje = self._vacia('algo_raro')

        assert 'algo_raro' in mensaje

    def test_una_respuesta_con_contenido_no_levanta_nada(self, app):
        """La comprobación no puede romper el camino que funciona."""
        from app.services.ai.openai_compatible import _explicar_respuesta_vacia

        # Solo se llama cuando el contenido está vacío; si alguien la llamara
        # de más, el mensaje sigue siendo el genérico y no una excepción rara.
        with pytest.raises(AIError):
            _explicar_respuesta_vacia('openai', 'm', {}, {})


class TestQueElErrorLlegaAlUsuario:
    def test_la_planificacion_lo_enseña(self, as_user, gestor, seeded, monkeypatch):
        """El error del proveedor tiene que salir en la pantalla, no en el
        registro: quien planifica no lee los logs."""
        from app.services import ai_service

        def _explota(*a, **k):
            raise AIError(
                'El modelo «x» se quedó sin espacio para responder. Suba '
                '«Máximo de tokens» en Administración → Proveedores de IA.'
            )

        monkeypatch.setattr(ai_service, 'plan_trip', _explota)

        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LON',
                'ida': '2026-11-01T09:00', 'vuelta': '2026-11-05T18:00',
                'viajeros': 1,
            }).get_data(as_text=True)

        assert 'Máximo de tokens' in html
