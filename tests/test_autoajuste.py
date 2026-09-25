"""El autoajuste: buscar parámetros mejores sin que nadie tenga que adivinarlos.

Lo que se prueba es lo que haría peligroso un ajuste automático si fallara:
que «mejor» signifique mejor y no más rápido a costa de acertar menos, que el
presupuesto se respete, que nada cambie en producción sin que alguien lo pida,
y que lo aplicado se pueda deshacer sin pisar un cambio posterior.
"""
import pytest

from app.models.ai import AIParameterProfile, AIRun
from app.models.enums import AIAutotuneState, AIProviderCode, AITask
from app.services import ai_provider_service, autotune_service, evaluation_service
from app.utils.errors import ConflictError, ValidationError

TAREA = AITask.SUMMARIZE_TRIP


@pytest.fixture
def simulado(app, admin):
    return ai_provider_service.create(
        admin, 'Simulado', AIProviderCode.STUB, modelo='stub-small', por_defecto=True,
    )


@pytest.fixture
def medida(monkeypatch):
    """Replace the measurement with a function of the parameters on trial.

    What is under test is the search, not the model; a real model would make
    every assertion here depend on its mood.
    """
    from app.services.ai import forzado_para

    estado = {'llamadas': 0, 'puntuar': None}

    def _medir(tarea, casos=None, repeticiones=1):
        estado['llamadas'] += 1
        parametros = forzado_para(tarea).parametros or {}
        calidad, duracion = estado['puntuar'](parametros)
        return {'calidad': calidad, 'duracion_ms': duracion, 'tokens': 10,
                'errores': 0, 'llamadas': 1, 'casos': []}

    monkeypatch.setattr(evaluation_service, 'medir', _medir)
    return estado


def _puntuar_temperatura_y_top_p(p):
    calidad = 50.0
    if p.get('temperatura') == 0.2:
        calidad += 30
    if p.get('top_p') == 0.9:
        calidad += 10
    return calidad, 1000


def _lanzar(admin, **kwargs):
    kwargs.setdefault('claves', ['temperatura', 'top_p'])
    kwargs.setdefault('presupuesto', 20)
    return autotune_service.lanzar(admin, TAREA, **kwargs)


@pytest.mark.unit
class TestQueEsMejor:
    def test_mas_calidad_gana(self):
        assert autotune_service.es_mejor(
            {'calidad': 80, 'duracion_ms': 900, 'errores': 0},
            {'calidad': 70, 'duracion_ms': 900, 'errores': 0},
        )

    def test_una_diferencia_dentro_del_ruido_no_gana(self):
        """The same setting scores differently on two runs."""
        assert not autotune_service.es_mejor(
            {'calidad': 70.5, 'duracion_ms': 900, 'errores': 0},
            {'calidad': 70.0, 'duracion_ms': 900, 'errores': 0},
        )

    def test_a_igual_calidad_gana_el_claramente_mas_rapido(self):
        assert autotune_service.es_mejor(
            {'calidad': 70, 'duracion_ms': 500, 'errores': 0},
            {'calidad': 70, 'duracion_ms': 1000, 'errores': 0},
        )

    def test_mas_rapido_pero_peor_no_gana(self):
        """That trade is somebody's decision, not the tuner's."""
        assert not autotune_service.es_mejor(
            {'calidad': 69.5, 'duracion_ms': 100, 'errores': 0},
            {'calidad': 70, 'duracion_ms': 1000, 'errores': 0},
        )


@pytest.mark.unit
class TestLaBusqueda:
    def test_encuentra_la_mejor_combinacion(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin)

        assert ajuste.estado is AIAutotuneState.COMPLETADO
        assert ajuste.parametros_mejores == {'temperatura': 0.2, 'top_p': 0.9}
        assert float(ajuste.calidad_base) == 50
        assert float(ajuste.calidad_mejor) == 90

    def test_guarda_todos_los_ensayos_no_solo_el_ganador(self, admin, simulado, medida):
        """«Why did it pick this?» has to be answerable by reading the row."""
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin)

        assert len(ajuste.ensayos) == medida['llamadas'] - 1, 'Menos el calentamiento.'
        assert ajuste.ensayos[0]['parametros'] == {}, 'El primero es la partida.'

    def test_respeta_el_presupuesto(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin, presupuesto=3)

        assert len(ajuste.ensayos) == 3
        assert medida['llamadas'] == 3 + 1, 'Los ensayos y un calentamiento.'

    def test_la_partida_no_paga_la_carga_del_modelo(self, admin, simulado, medida):
        """Found against a real Ollama: the first call loads the model.

        Without an uncounted call first, the baseline was always the slowest
        trial, and «equal quality, clearly faster» crowned the second one.
        """
        def _primera_lenta(p):
            return 70.0, (30000 if medida['llamadas'] == 1 else 23000)

        medida['puntuar'] = _primera_lenta

        ajuste = _lanzar(admin)

        assert ajuste.ensayos[0]['duracion_ms'] == 23000
        assert not ajuste.hay_propuesta

    def test_sin_mejora_no_hay_propuesta(self, admin, simulado, medida):
        medida['puntuar'] = lambda p: (70.0, 1000)

        ajuste = _lanzar(admin)

        assert not ajuste.hay_propuesta

    def test_parte_del_perfil_que_ya_tiene_la_tarea(self, admin, simulado, medida):
        ai_provider_service.save_profile(admin, simulado, 'stub-small', TAREA, {'seed': 7})
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin)

        assert ajuste.parametros_base == {'seed': 7}
        assert ajuste.parametros_mejores['seed'] == 7

    def test_un_fallo_queda_explicado(self, admin, simulado, medida):
        def _revienta(p):
            raise RuntimeError('el proveedor se cayó')

        medida['puntuar'] = _revienta

        ajuste = _lanzar(admin)

        assert ajuste.estado is AIAutotuneState.ERROR
        assert 'se cayó' in ajuste.error

    def test_se_puede_cancelar_a_medias(self, admin, simulado, medida):
        from app.extensions import db
        from app.models.ai import AIAutotuneRun

        def _cancela_en_el_segundo(p):
            if medida['llamadas'] == 3:
                fila = AIAutotuneRun.query.one()
                fila.estado = AIAutotuneState.CANCELADO
                db.session.commit()
            return 50.0, 1000

        medida['puntuar'] = _cancela_en_el_segundo

        ajuste = _lanzar(admin)

        assert ajuste.estado is AIAutotuneState.CANCELADO
        # The trial that was running when it was cancelled is not kept.
        assert len(ajuste.ensayos) == 1


@pytest.mark.unit
class TestLoQueSeNiega:
    def test_una_tarea_sin_forma_de_medirse(self, admin, simulado):
        with pytest.raises(ValidationError, match='no tiene todavía forma de medirse'):
            autotune_service.lanzar(admin, AITask.ANALYZE_RISKS)

    def test_una_tarea_sin_casos(self, admin, simulado, monkeypatch):
        """Tuning against nothing would «optimise» noise."""
        monkeypatch.setattr(evaluation_service, 'casos_de', lambda tarea: [])

        with pytest.raises(ValidationError, match='No hay casos'):
            autotune_service.lanzar(admin, TAREA)

    def test_dos_a_la_vez_sobre_la_misma_tarea(self, admin, simulado, monkeypatch):
        monkeypatch.setattr(autotune_service, '_despachar', lambda ajuste: None)
        autotune_service.lanzar(admin, TAREA)

        with pytest.raises(ConflictError, match='en marcha'):
            autotune_service.lanzar(admin, TAREA)

    def test_un_parametro_que_el_proveedor_no_admite(self, admin):
        openai = ai_provider_service.create(
            admin, 'OpenAI', AIProviderCode.OPENAI, api_key='sk-1', por_defecto=True,
        )
        with pytest.raises(ValidationError, match='no admite ajustar'):
            autotune_service.lanzar(admin, TAREA, config=openai, claves=['top_k'])

    def test_un_presupuesto_desmedido(self, admin, simulado):
        with pytest.raises(ValidationError, match='ensayos'):
            autotune_service.lanzar(admin, TAREA, presupuesto=500)


@pytest.mark.unit
class TestAplicarYRevertir:
    def test_por_defecto_es_solo_una_propuesta(self, admin, simulado, medida):
        """Nothing changes in production unless somebody asked for it."""
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin)

        assert ajuste.hay_propuesta and not ajuste.aplicado
        assert AIParameterProfile.query.count() == 0

    def test_aplicar_escribe_el_perfil_de_la_tarea(self, admin, simulado, medida):
        from app.services.ai.selector import _params, componer

        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)

        autotune_service.aplicar(admin, ajuste)

        efectivos, origen = componer(TAREA, simulado, 'stub-small', 'stub',
                                     generales=_params(None, simulado, 'stub-small'))
        assert efectivos['temperatura'] == 0.2 and efectivos['top_p'] == 0.9
        assert origen['top_p'] == 'Perfil de la tarea'
        perfil = AIParameterProfile.query.one()
        assert perfil.autotune_run_id == ajuste.id

    def test_revertir_deja_lo_que_habia(self, admin, simulado, medida):
        ai_provider_service.save_profile(admin, simulado, 'stub-small', TAREA, {'seed': 7})
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)
        autotune_service.aplicar(admin, ajuste)

        autotune_service.revertir(admin, ajuste)

        assert AIParameterProfile.query.one().parametros == {'seed': 7}

    def test_revertir_sin_perfil_previo_lo_quita(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)
        autotune_service.aplicar(admin, ajuste)

        autotune_service.revertir(admin, ajuste)

        assert AIParameterProfile.query.count() == 0

    def test_no_revierte_encima_de_un_cambio_posterior(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)
        autotune_service.aplicar(admin, ajuste)
        ai_provider_service.save_profile(admin, simulado, 'stub-small', TAREA, {'top_p': 0.5})

        with pytest.raises(ConflictError, match='cambiado después'):
            autotune_service.revertir(admin, ajuste)

    def test_se_aplica_solo_si_se_pidio_y_mejora_bastante(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        ajuste = _lanzar(admin, aplicar_si_mejora=True)

        assert ajuste.aplicado

    def test_una_mejora_dentro_del_ruido_no_se_aplica_sola(self, admin, simulado, medida):
        medida['puntuar'] = lambda p: (51.5 if p.get('temperatura') == 0.2 else 50.0, 1000)
        ajuste = _lanzar(admin, aplicar_si_mejora=True)

        # 1.5 points clears the search's tolerance but not the automatic bar.
        assert ajuste.hay_propuesta
        assert not ajuste.aplicado

    def test_queda_en_la_auditoria(self, admin, simulado, medida):
        from app.models.audit import AuditEvent

        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)
        autotune_service.aplicar(admin, ajuste)

        acciones = {e.accion for e in AuditEvent.query.all()}
        assert {'ai_autotune.launched', 'ai_autotune.completed',
                'ai_autotune.applied'} <= acciones


@pytest.mark.integration
class TestDeVerdadContraElProveedorSimulado:
    """Without replacing the measurement: the real task, the real run log."""

    def test_cada_ensayo_queda_registrado_como_autoajuste(self, admin, simulado):
        ajuste = autotune_service.lanzar(
            admin, AITask.EXPLAIN_ALERT, claves=['temperatura'], presupuesto=2,
        )

        assert ajuste.estado is AIAutotuneState.COMPLETADO, ajuste.error
        runs = AIRun.query.filter_by(tarea=AITask.EXPLAIN_ALERT.value).all()
        assert runs
        assert all(r.finalidad.startswith('Autoajuste') for r in runs)
        assert {r.parametros['temperatura'] for r in runs} >= {0.2, 0.0}

    def test_el_ajuste_no_se_queda_puesto_despues(self, admin, simulado):
        from app.services.ai.selector import forzado_para

        autotune_service.lanzar(admin, AITask.EXPLAIN_ALERT, claves=['temperatura'],
                                presupuesto=2)

        assert forzado_para(AITask.EXPLAIN_ALERT) is None


@pytest.mark.unit
class TestLaMedida:
    def test_los_criterios_cuentan_lo_que_se_cumple(self):
        datos = {'resumen': 'Vuelo VY7604 a Londres', 'datos_insuficientes': False}

        nota = evaluation_service.puntuar_criterios(datos, {
            'debe_mencionar': ['VY7604', 'Hotel'],
            'no_debe_mencionar': ['Madrid'],
            'campos': {'datos_insuficientes': False},
        })

        assert nota == 75.0

    def test_sin_criterios_basta_con_cumplir_el_esquema(self):
        assert evaluation_service.puntuar_criterios({'a': 1}, {}) == 100.0

    def test_un_error_puntua_cero_y_se_cuenta(self, app, simulado, monkeypatch):
        from app.services import ai_service

        def _revienta(*a, **k):
            raise RuntimeError('no respondió')

        monkeypatch.setattr(ai_service, '_run', _revienta)

        medida = evaluation_service.medir(AITask.SUMMARIZE_TRIP)

        assert medida['calidad'] == 0
        assert medida['errores'] == medida['llamadas'] > 0

    def test_hay_casos_para_cada_tarea_evaluable(self):
        for tarea in evaluation_service.EVALUABLES:
            assert evaluation_service.casos_de(tarea), f'{tarea} no tiene casos'

    def test_los_casos_son_sinteticos(self):
        """They live in the repository: no real person's trip goes in them."""

        # The documents of the golden set are checked in test_evaluacion.
        for ruta in evaluation_service.CASOS_POR_TAREA.rglob('*.json'):
            texto = ruta.read_text(encoding='utf-8').lower()
            assert 'ejemplo' in texto, ruta.name
            assert '@gmail.com' not in texto, ruta.name


@pytest.mark.unit
class TestElAutoajusteSemanal:
    def test_apagado_por_defecto(self, app, seeded, simulado):
        """Every search is model calls, which on an external provider is money."""
        assert autotune_service.autoajuste_periodico() == []

    def test_encendido_lanza_uno_por_tarea_con_casos(self, app, seeded, simulado, medida):
        from app.services import settings_service

        settings_service.set_value('IA_AUTOAJUSTE_PERIODICO', True)
        medida['puntuar'] = lambda p: (70.0, 1000)

        lanzados = autotune_service.autoajuste_periodico()

        assert {str(a.tarea) for a in lanzados} == set(evaluation_service.EVALUABLES)
        assert all(a.automatico and not a.aplicado for a in lanzados)


@pytest.mark.integration
class TestLasPantallasDelAutoajuste:
    def test_se_ve_y_se_lanza(self, as_user, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p

        with as_user(admin) as client:
            assert client.get('/admin/ajustes/ia/autoajuste').status_code == 200
            respuesta = client.post('/admin/ajustes/ia/autoajuste', data={
                'tarea': TAREA.value, 'claves': ['temperatura', 'top_p'],
                'presupuesto': '20', 'repeticiones': '1',
            })
            assert respuesta.status_code == 302
            detalle = client.get(respuesta.headers['Location'])

        assert detalle.status_code == 200
        assert 'Aplicar' in detalle.get_data(as_text=True)

    def test_se_llega_desde_los_proveedores(self, as_user, admin, simulado):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes/proveedores').get_data(as_text=True)

        assert '/admin/ajustes/ia/autoajuste' in html

    def test_un_gestor_no_entra(self, as_user, gestor, simulado):
        with as_user(gestor) as client:
            assert client.get('/admin/ajustes/ia/autoajuste').status_code in (403, 404)


@pytest.mark.integration
class TestElSeguimientoNoAgotaElLimite:
    """Found in production: a 429 half an hour into a search.

    The detail page reloaded itself every 15 seconds -- 240 requests an hour
    against a default allowance of 100 per route. It now asks a small status
    endpoint with its own limit, and reloads only when there is something new.
    """

    @pytest.fixture
    def en_curso(self, admin, simulado, monkeypatch):
        monkeypatch.setattr(autotune_service, '_despachar', lambda ajuste: None)
        return autotune_service.lanzar(admin, TAREA)

    def test_la_pagina_no_se_recarga_entera_sola(self, as_user, admin, en_curso):
        with as_user(admin) as client:
            html = client.get(
                f'/admin/ajustes/ia/autoajuste/{en_curso.id}'
            ).get_data(as_text=True)

        assert 'http-equiv="refresh"' not in html
        assert f'/admin/ajustes/ia/autoajuste/{en_curso.id}/estado' in html

    def test_el_estado_dice_lo_justo(self, as_user, admin, en_curso):
        with as_user(admin) as client:
            datos = client.get(f'/admin/ajustes/ia/autoajuste/{en_curso.id}/estado').get_json()

        assert datos == {'estado': 'pendiente', 'activo': True, 'ensayos': 0,
                         'parado': False}

    def test_terminado_ya_no_pregunta(self, as_user, admin, en_curso):
        autotune_service.cancelar(admin, en_curso)

        with as_user(admin) as client:
            html = client.get(
                f'/admin/ajustes/ia/autoajuste/{en_curso.id}'
            ).get_data(as_text=True)

        assert '/estado' not in html

    def test_el_estado_tiene_su_propio_limite(self):
        """The default 100 per hour is below one poll every 15 seconds."""
        import inspect

        from app.blueprints.admin import routes

        fuente = inspect.getsource(routes.ai_autotune_status)
        assert "@limiter.limit('600 per hour')" in fuente

    def test_un_gestor_no_consulta_el_estado(self, as_user, gestor, en_curso):
        with as_user(gestor) as client:
            respuesta = client.get(f'/admin/ajustes/ia/autoajuste/{en_curso.id}/estado')

        assert respuesta.status_code in (403, 404)


@pytest.mark.unit
class TestUnaBusquedaSobreviveASuTrabajador:
    """Found in production: the worker's 30-minute limit killed a search at
    trial 8 of 12, and the row said «en curso» for ever.

    Each step now runs one trial and everything the search knows is in the
    row, so a step after a crash, a restart or «Reanudar» carries on.
    """

    @pytest.fixture
    def en_cola(self, admin, simulado, medida, monkeypatch):
        monkeypatch.setattr(autotune_service, '_despachar', lambda ajuste: None)
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        return _lanzar(admin)

    def test_un_paso_es_un_ensayo(self, en_cola, medida):
        autotune_service.paso(en_cola.id)          # arranca y calienta
        antes = medida['llamadas']

        autotune_service.paso(en_cola.id)

        assert medida['llamadas'] == antes + 1
        assert len(autotune_service.get_or_404(en_cola.id).ensayos) == 1

    def test_tras_una_caida_sigue_donde_estaba(self, admin, en_cola, medida):
        for _ in range(4):
            autotune_service.paso(en_cola.id)
        hechos = [e['parametros'] for e in autotune_service.get_or_404(en_cola.id).ensayos]
        # Here the worker dies. Whatever runs next only has the row.

        ajuste = autotune_service.ejecutar(en_cola.id)

        assert [e['parametros'] for e in ajuste.ensayos][:len(hechos)] == hechos
        assert ajuste.parametros_mejores == {'temperatura': 0.2, 'top_p': 0.9}
        assert len({str(e['parametros']) for e in ajuste.ensayos}) == len(ajuste.ensayos), (
            'Ningún ensayo se repite al reanudar.'
        )

    def test_da_lo_mismo_que_sin_interrupcion(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        seguido = _lanzar(admin)
        autotune_service.cancelar(admin, seguido) if seguido.activo else None

        assert seguido.parametros_mejores == {'temperatura': 0.2, 'top_p': 0.9}

    def test_un_paso_sin_el_testigo_no_toca_nada(self, en_cola):
        from app.extensions import db

        en_cola.celery_task_id = 'el-bueno'
        db.session.commit()

        assert autotune_service.paso(en_cola.id, token='otro') is False
        assert autotune_service.get_or_404(en_cola.id).estado is AIAutotuneState.PENDIENTE

    def test_un_paso_relevado_descarta_su_ensayo(self, en_cola, medida, monkeypatch):
        """If the old step was only slow, its trial must not be recorded twice."""
        from app.extensions import db
        from app.models.ai import AIAutotuneRun

        en_cola.celery_task_id = 'viejo'
        db.session.commit()
        autotune_service.paso(en_cola.id, token='viejo')

        def _relevado_a_medias(p):
            fila = AIAutotuneRun.query.one()
            fila.celery_task_id = 'nuevo'
            db.session.commit()
            return 70.0, 1000

        medida['puntuar'] = _relevado_a_medias

        assert autotune_service.paso(en_cola.id, token='viejo') is False
        assert autotune_service.get_or_404(en_cola.id).ensayos == []

    def test_el_relevo_solo_lo_da_quien_tiene_el_testigo(self, en_cola, monkeypatch):
        from app.extensions import db
        from app.tasks import ai_tasks

        encolados = []
        monkeypatch.setattr(ai_tasks.run_autotune, 'apply_async',
                            lambda args, task_id: encolados.append(task_id))
        en_cola.celery_task_id = 'actual'
        db.session.commit()

        assert autotune_service.continuar(en_cola.id, 'otro') is False
        assert autotune_service.continuar(en_cola.id, 'actual') is True
        assert encolados == [autotune_service.get_or_404(en_cola.id).celery_task_id]

    def test_el_limite_de_tiempo_no_se_confunde_con_un_caso_fallido(
        self, app, simulado, monkeypatch,
    ):
        """Swallowed, the search ran on to the hard limit and died silently."""
        from celery.exceptions import SoftTimeLimitExceeded

        from app.services import ai_service

        def _se_acaba_el_tiempo(*a, **k):
            raise SoftTimeLimitExceeded()

        monkeypatch.setattr(ai_service, '_run', _se_acaba_el_tiempo)

        with pytest.raises(SoftTimeLimitExceeded):
            evaluation_service.medir(AITask.SUMMARIZE_TRIP)

    def test_la_tarea_cierra_con_explicacion_si_un_ensayo_no_cabe(
        self, en_cola, monkeypatch,
    ):
        from celery.exceptions import SoftTimeLimitExceeded

        from app.tasks import ai_tasks

        def _se_acaba_el_tiempo(*a, **k):
            raise SoftTimeLimitExceeded()

        monkeypatch.setattr(autotune_service, 'paso', _se_acaba_el_tiempo)

        # The body, not .apply(): that would open its own application
        # context, with its own in-memory database.
        ai_tasks.run_autotune.run(str(en_cola.id))

        ajuste = autotune_service.get_or_404(en_cola.id)
        assert ajuste.estado is AIAutotuneState.ERROR
        assert 'tardó más' in ajuste.error

    def test_la_tarea_tiene_su_propio_limite(self):
        """The global 30 minutes is what killed it."""
        from app.tasks import ai_tasks

        assert ai_tasks.run_autotune.soft_time_limit > 1800


@pytest.mark.integration
class TestReanudar:
    @pytest.fixture
    def parado(self, admin, simulado, medida, monkeypatch):
        from datetime import timedelta

        from app.extensions import db
        from app.utils.timeutil import utcnow

        monkeypatch.setattr(autotune_service, '_despachar', lambda ajuste: None)
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)
        autotune_service.paso(ajuste.id)
        autotune_service.paso(ajuste.id)
        db.session.execute(
            db.update(type(ajuste)).where(type(ajuste).id == ajuste.id)
            .values(updated_at=utcnow() - timedelta(hours=2))
        )
        db.session.commit()
        return autotune_service.get_or_404(ajuste.id)

    def test_se_ofrece_cuando_no_avanza(self, as_user, admin, parado):
        assert autotune_service.parece_parado(parado)

        with as_user(admin) as client:
            html = client.get(f'/admin/ajustes/ia/autoajuste/{parado.id}').get_data(as_text=True)

        assert 'Reanudar' in html

    def test_no_se_ofrece_mientras_avanza(self, admin, simulado, medida, monkeypatch):
        monkeypatch.setattr(autotune_service, '_despachar', lambda ajuste: None)
        ajuste = _lanzar(admin)

        assert not autotune_service.parece_parado(ajuste)

    def test_reanudar_encola_un_paso_nuevo_con_otro_testigo(
        self, as_user, admin, parado, monkeypatch,
    ):
        from app.tasks import ai_tasks

        encolados = []
        monkeypatch.setattr(ai_tasks.run_autotune, 'apply_async',
                            lambda args, task_id: encolados.append(task_id))
        antes = parado.celery_task_id

        with as_user(admin) as client:
            client.post(f'/admin/ajustes/ia/autoajuste/{parado.id}/reanudar')

        despues = autotune_service.get_or_404(parado.id).celery_task_id
        assert encolados == [despues]
        assert despues != antes

    def test_uno_terminado_no_se_reanuda(self, admin, simulado, medida):
        medida['puntuar'] = _puntuar_temperatura_y_top_p
        ajuste = _lanzar(admin)

        with pytest.raises(ValidationError, match='terminado'):
            autotune_service.reanudar(admin, ajuste)
