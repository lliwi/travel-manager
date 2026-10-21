"""The allow-list of public sources, edited from Administración.

The research service fetches nothing outside this list, so an edit here
widens or narrows what the application may reach on the internet. The
rules pinned below are the ones that keep an entry meaning one site.
"""
import pytest

from app.models.advisory import WebFetch, WebSource
from app.services import web_research_service, web_source_service
from app.utils.errors import ConflictError, ValidationError

pytestmark = pytest.mark.security


def _datos(**extra):
    return {'nombre': 'Embajada', 'dominio': 'embajada.example', 'categoria': 'seguridad',
            'activa': True, 'prioridad': 50, 'confianza_base': 0.8, **extra}


class TestElDominio:
    @pytest.mark.parametrize('escrito, guardado', [
        ('exteriores.gob.es', 'exteriores.gob.es'),
        ('https://www.Exteriores.gob.es/es/pagina?x=1', 'exteriores.gob.es'),
        ('travel.state.gov.', 'travel.state.gov'),
        ('cdc.gov:443', 'cdc.gov'),
    ])
    def test_se_limpia_lo_pegado(self, escrito, guardado):
        assert web_source_service.normalizar_dominio(escrito) == guardado

    @pytest.mark.parametrize('escrito', [
        '', '*.gob.es', 'es', '10.0.0.1', '[::1]', 'mal_dominio.com', '-x.com',
    ])
    def test_lo_que_abriria_mas_de_un_sitio_se_rechaza(self, escrito):
        with pytest.raises(ValidationError):
            web_source_service.normalizar_dominio(escrito)


class TestAltaYEdicion:
    def test_una_fuente_nueva_queda_autorizada(self, app, seeded, admin):
        web_source_service.crear(admin, _datos())

        assert web_research_service.allowed_source('https://www.embajada.example/x')

    def test_un_dominio_repetido_se_rechaza(self, app, seeded, admin):
        with pytest.raises(ConflictError):
            web_source_service.crear(admin, _datos(dominio='https://exteriores.gob.es/'))

    def test_la_pagina_de_inicio_tiene_que_estar_en_el_dominio(self, app, seeded, admin):
        with pytest.raises(ValidationError):
            web_source_service.crear(
                admin, _datos(url_base='https://otro.example/recomendaciones'),
            )

    def test_desactivada_deja_de_consultarse(self, app, seeded, admin):
        fuente = web_source_service.crear(admin, _datos())
        web_source_service.cambiar_estado(admin, fuente, False)

        assert web_research_service.allowed_source('https://embajada.example/') is None

    def test_cada_cambio_queda_auditado(self, app, seeded, admin):
        from app.models.audit import AuditEvent

        fuente = web_source_service.crear(admin, _datos())
        web_source_service.actualizar(admin, fuente, _datos(nombre='Consulado'))

        acciones = [e.accion for e in AuditEvent.query.all()]
        assert 'web_source.created' in acciones
        assert 'web_source.updated' in acciones


class TestEliminar:
    def test_una_fuente_propia_sin_uso_se_elimina(self, app, seeded, admin):
        fuente = web_source_service.crear(admin, _datos())

        web_source_service.eliminar(admin, fuente)

        assert WebSource.query.filter_by(dominio='embajada.example').first() is None

    def test_una_de_serie_no_porque_volveria_al_arrancar(self, app, seeded, admin):
        fuente = WebSource.query.filter_by(dominio='exteriores.gob.es').one()

        with pytest.raises(ConflictError, match='Desactívela'):
            web_source_service.eliminar(admin, fuente)

    def test_una_ya_consultada_no_porque_la_citan(self, app, seeded, admin):
        from app.extensions import db

        fuente = web_source_service.crear(admin, _datos())
        db.session.add(WebFetch(web_source_id=fuente.id, url='https://embajada.example/',
                                url_hash='0' * 64))
        db.session.commit()

        with pytest.raises(ConflictError, match='Desactívela'):
            web_source_service.eliminar(admin, fuente)


@pytest.mark.integration
class TestLaPantalla:
    def test_el_administrador_la_ve(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/fuentes').get_data(as_text=True)

        assert 'exteriores.gob.es' in html

    def test_se_da_de_alta_desde_el_formulario(self, as_user, admin, seeded):
        with as_user(admin) as client:
            respuesta = client.post('/admin/fuentes/nueva', data={
                'nombre': 'Embajada', 'dominio': 'https://www.embajada.example/',
                'categoria': 'seguridad', 'prioridad': '50',
                'confianza_base': '0.8', 'activa': 'y',
            })

        assert respuesta.status_code == 302
        assert WebSource.query.filter_by(dominio='embajada.example').one().activa

    def test_se_edita_desde_el_formulario(self, as_user, admin, seeded):
        fuente = WebSource.query.filter_by(dominio='exteriores.gob.es').one()

        with as_user(admin) as client:
            client.post(f'/admin/fuentes/{fuente.id}/editar', data={
                'nombre': 'Exteriores', 'dominio': 'exteriores.gob.es',
                'categoria': 'sanidad', 'prioridad': '10', 'confianza_base': '0.9',
            })

        assert fuente.nombre == 'Exteriores'
        assert str(fuente.categoria) == 'sanidad'
        assert fuente.activa is False

    def test_se_llega_desde_los_ajustes(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'href="/admin/fuentes"' in html

    def test_un_gestor_no_entra(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            assert client.get('/admin/fuentes').status_code in (403, 404)
