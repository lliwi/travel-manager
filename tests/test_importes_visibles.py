"""Dónde se puede escribir un importe, y por qué a veces no se puede.

Los costes son opcionales por especificación (§2.2, «si se habilita su
tratamiento»), así que viven tras «COSTES_HABILITADOS» y salen apagados. El
efecto es que quien busca dónde anotar un importe no encuentra el campo en
ninguna pantalla y tampoco nada que le diga que existe un interruptor.

Esto fija las dos mitades: con el interruptor apagado no aparece en ninguna
parte, y encendido aparece en todas. Si alguien añade mañana una pantalla de
itinerario y olvida el campo, aquí se nota.
"""
import pytest

from app.models.enums import TripStatus
from app.services import settings_service, trip_service

pytestmark = pytest.mark.integration

#: Cada clase de elemento del itinerario tiene su propio formulario, y el
#: campo compartido se olvida en uno con facilidad.
CLASES = ('segmento', 'alojamiento', 'vehiculo', 'servicio')


@pytest.fixture
def viaje(gestor, seeded):
    return trip_service.create_trip(
        gestor, titulo='Para importes', estado=TripStatus.CONFIRMADO,
    )


def _pantallas(client, viaje):
    """Las pantallas donde se anota dinero, con su nombre para el fallo."""
    paginas = {
        'Editar viaje': client.get(f'/trips/{viaje.id}/editar'),
    }
    for kind in CLASES:
        paginas[f'Nuevo {kind}'] = client.get(
            f'/trips/{viaje.id}/itinerario/{kind}/nuevo')
    return {
        nombre: respuesta.get_data(as_text=True)
        for nombre, respuesta in paginas.items()
    }


class TestConLosCostesApagados:
    def test_no_se_pide_un_importe_en_ninguna_pantalla(self, as_user, gestor, viaje):
        settings_service.set_value('COSTES_HABILITADOS', False)

        with as_user(gestor) as client:
            paginas = _pantallas(client, viaje)

        con_campo = [n for n, html in paginas.items()
                     if 'name="importe"' in html or 'name="coste_estimado"' in html]

        assert not con_campo, f'Piden importe con los costes apagados: {con_campo}'


class TestConLosCostesEncendidos:
    def test_se_pide_en_todas(self, as_user, gestor, viaje):
        """El campo está en el formulario compartido, pero cada clase tiene su
        plantilla y olvidarlo en una es fácil."""
        settings_service.set_value('COSTES_HABILITADOS', True)

        with as_user(gestor) as client:
            paginas = _pantallas(client, viaje)

        sin_campo = [n for n, html in paginas.items()
                     if 'name="importe"' not in html and 'name="coste_estimado"' not in html]

        assert not sin_campo, f'No piden importe con los costes encendidos: {sin_campo}'

    def test_y_tambien_la_moneda(self, as_user, gestor, viaje):
        """Un importe sin moneda no se puede sumar con otro."""
        settings_service.set_value('COSTES_HABILITADOS', True)

        with as_user(gestor) as client:
            paginas = _pantallas(client, viaje)

        sin_moneda = [n for n, html in paginas.items() if 'name="moneda"' not in html]

        assert not sin_moneda, f'Piden importe sin moneda: {sin_moneda}'

    def test_lo_escrito_se_guarda(self, as_user, gestor, viaje):
        from app.models.itinerary import Accommodation

        settings_service.set_value('COSTES_HABILITADOS', True)

        with as_user(gestor) as client:
            client.post(f'/trips/{viaje.id}/itinerario/alojamiento/nuevo', data={
                'csrf_token': 'x', 'nombre': 'Hotel', 'ciudad': 'Londres',
                'importe': '327.03', 'moneda': 'EUR',
            }, follow_redirects=True)

        hotel = Accommodation.query.filter_by(trip_id=viaje.id).first()

        assert hotel is not None
        assert float(hotel.importe) == pytest.approx(327.03)
        assert hotel.moneda == 'EUR'


class TestElInterruptorEsAlcanzable:
    def test_sale_en_ajustes(self, as_user, admin, seeded):
        """Si no estuviera, los costes serían una funcionalidad apagada sin
        forma de encenderla."""
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'COSTES_HABILITADOS' in html
