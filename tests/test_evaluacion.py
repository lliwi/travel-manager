"""Measuring the model instead of arguing about it.

Two halves answering different questions. «¿Está funcionando?» comes from the
runs already recorded and costs nothing. «¿Es correcto?» cannot: nothing in a
run says whether the answer was right, so it needs documents whose correct
extraction somebody wrote down once.
"""

import pytest

from app.extensions import db
from app.services import evaluation_service


@pytest.mark.unit
class TestLaComparacionCampoACampo:
    def test_lo_igual_acierta(self):
        r = evaluation_service.comparar({'a': 'BCN'}, {'a': 'BCN'})

        assert r['aciertos'] == ['a']
        assert r['porcentaje'] == 100

    def test_no_falla_por_mayusculas_ni_espacios(self):
        """A golden set that fails on whitespace teaches people to ignore it."""
        r = evaluation_service.comparar({'a': 'BCN'}, {'a': ' bcn '})

        assert r['aciertos'] == ['a']

    def test_lo_distinto_falla_y_dice_qué_esperaba(self):
        r = evaluation_service.comparar({'a': 'BCN'}, {'a': 'MAD'})

        assert r['fallos'][0] == {'campo': 'a', 'esperado': 'BCN', 'obtenido': 'MAD'}

    def test_lo_ausente_se_distingue_de_lo_equivocado(self):
        """Not answering and answering wrongly are different failures."""
        r = evaluation_service.comparar({'a': 'BCN', 'b': 'LGW'}, {'a': None, 'b': 'MAD'})

        assert r['ausentes'] == ['a']
        assert [f['campo'] for f in r['fallos']] == ['b']

    def test_un_instante_se_compara_por_su_hora_local(self):
        r = evaluation_service.comparar(
            {'salida': '2026-10-31T07:55'},
            {'salida': {'local': '2026-10-31T07:55', 'zona_horaria': 'Europe/Madrid'}},
        )

        assert r['aciertos'] == ['salida']

    def test_sin_campos_esperados_no_puntua(self):
        assert evaluation_service.comparar({}, {'a': 'x'})['porcentaje'] == 0


@pytest.mark.unit
class TestElConjuntoDorado:
    def test_hay_casos_en_el_repositorio(self):
        """Without them, changing model is a leap of faith dressed as config."""
        casos = evaluation_service.casos_dorados()

        assert casos, 'data/evaluacion debería traer casos de ejemplo.'

    def test_cada_caso_apunta_a_un_documento_que_existe(self):
        for nombre, documento, _caso in evaluation_service.casos_dorados():
            assert documento.is_file(), f'{nombre} apunta a un fichero que no está'

    def test_cada_caso_declara_lo_que_espera(self):
        for nombre, _doc, caso in evaluation_service.casos_dorados():
            assert caso.get('esperado'), f'{nombre} no dice qué debería salir'
            assert caso.get('clasificacion'), f'{nombre} no dice de qué tipo es'

    def test_los_documentos_son_sinteticos(self):
        """A golden set lives in the repository and is read at every review.

        Putting somebody's real booking in it would publish their data for
        ever in exchange for a convenience.
        """
        for _nombre, documento, _caso in evaluation_service.casos_dorados():
            texto = documento.read_text(encoding='utf-8')
            assert '@gmail.com' not in texto
            assert 'example' in texto.lower() or 'ejemplo' in texto.lower()


@pytest.mark.unit
class TestLaEvaluacionNoDejaRastro:
    def test_no_crea_documentos(self, app, seeded, monkeypatch):
        """A measurement that changes what it measures is not one."""
        from app.models.document import Document
        from app.services import ai_service

        monkeypatch.setattr(ai_service, 'extract_document', lambda *a, **k: {
            'datos': {'servicios': [{'campos': {'numero_vuelo': 'VY7604'}}]},
        })
        antes = Document.query.count()

        evaluation_service.evaluar()

        assert Document.query.count() == antes

    def test_un_fallo_del_modelo_puntua_cero_y_no_revienta(
        self, app, seeded, monkeypatch
    ):
        from app.services import ai_service

        def _revienta(*a, **k):
            raise RuntimeError('el proveedor no respondió')

        monkeypatch.setattr(ai_service, 'extract_document', _revienta)

        resultado = evaluation_service.evaluar()

        assert resultado['porcentaje'] == 0
        assert all(c['error'] for c in resultado['casos'])

    def test_sin_casos_devuelve_cero_sin_quejarse(self, app, seeded):
        resultado = evaluation_service.evaluar(casos=[])

        assert resultado['total'] == 0


@pytest.mark.unit
class TestElRendimientoDeLoQueYaPaso:
    def _run(self, modelo, estado, duracion=1000, tarea='extract_document'):
        from app.models.ai import AIRun
        from app.models.enums import AITask

        db.session.add(AIRun(
            tarea=AITask.coerce(tarea), proveedor='openai', modelo=modelo,
            estado=estado, duracion_ms=duracion,
            tokens_entrada=10, tokens_salida=5,
        ))
        db.session.commit()

    def test_se_resume_por_modelo(self, app, seeded):
        from app.models.enums import AIRunState

        self._run('modelo-a', AIRunState.COMPLETADA)
        self._run('modelo-a', AIRunState.ERROR)

        filas = {f['modelo']: f for f in evaluation_service.rendimiento_por_modelo()}

        assert filas['modelo-a']['total'] == 2
        assert filas['modelo-a']['tasa_error'] == 50.0

    def test_el_peor_sale_primero(self, app, seeded):
        from app.models.enums import AIRunState

        self._run('bueno', AIRunState.COMPLETADA)
        self._run('malo', AIRunState.ERROR)

        filas = evaluation_service.rendimiento_por_modelo()

        assert filas[0]['modelo'] == 'malo'

    def test_la_duracion_media_pondera_por_ejecuciones(self, app, seeded):
        """Or one slow failure would count as much as a hundred fast successes."""
        from app.models.enums import AIRunState

        for _ in range(9):
            self._run('mixto', AIRunState.COMPLETADA, duracion=100)
        self._run('mixto', AIRunState.ERROR, duracion=10000)

        filas = {f['modelo']: f for f in evaluation_service.rendimiento_por_modelo()}

        assert 1000 <= filas['mixto']['duracion_media_ms'] <= 1100
