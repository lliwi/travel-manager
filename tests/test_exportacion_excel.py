"""The trips as a spreadsheet for whoever keeps the accounts.

What must hold without anybody watching: only those who manage get the file,
it holds what they could open on screen and no more, amounts follow the cost
switch, and nothing in a cell is run by the spreadsheet that opens it.
"""
import io
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

from app.services import (
    cuenta_contable_service,
    export_service,
    itinerary_service,
    settings_service,
    trip_service,
)


@pytest.fixture
def viaje(admin, gestor, viajero):
    settings_service.set_value('COSTES_HABILITADOS', True)
    cuenta_contable_service.guardar(admin, {'vuelo': ('6290001', 'Desplazamientos')})
    trip = trip_service.create_trip(
        gestor, titulo='Auditoría en Lyon', proyecto='PRJ-0042', facturable=True,
        inicio_local=datetime(2026, 12, 1), fin_local=datetime(2026, 12, 3, 23, 59),
        inicio_tz='Europe/Madrid',
    )
    trip_service.add_traveler(gestor, trip, viajero)
    itinerary_service.create_item(
        gestor, trip, 'segmento', numero='VY1234',
        instants={'salida': (datetime(2026, 12, 1, 7, 10), 'Europe/Madrid'),
                  'llegada': (datetime(2026, 12, 1, 9, 0), 'Europe/Paris')},
        origen_codigo='BCN', destino_codigo='LYS', importe=180, moneda='EUR',
    )
    itinerary_service.create_item(
        gestor, trip, 'alojamiento', nombre='=HYPERLINK("http://malo.test")',
        instants={'check_in': (datetime(2026, 12, 1, 15, 0), 'Europe/Paris'),
                  'check_out': (datetime(2026, 12, 3, 11, 0), 'Europe/Paris')},
        importe=320, moneda='EUR',
    )
    itinerary_service.create_item(
        gestor, trip, 'servicio', tipo='dietas', nombre='Dietas sin valorar',
    )
    return trip


def _hojas(actor, **kwargs):
    contenido, nombre, resumen = export_service.exportar(actor, **kwargs)
    libro = load_workbook(io.BytesIO(contenido))
    hojas = {
        h.title: [dict(zip([c.value for c in h[1]], [c.value for c in fila], strict=True))
                  for fila in h.iter_rows(min_row=2)]
        for h in libro.worksheets
    }
    return hojas, nombre, resumen


class TestElContenido:
    def test_una_fila_por_gasto_con_lo_que_contabilidad_pide(self, gestor, viaje):
        hojas, nombre, resumen = _hojas(gestor)

        vuelo = next(f for f in hojas['Gastos'] if f['Tipo de gasto'] == 'Vuelo')
        assert vuelo['Proyecto'] == 'PRJ-0042'
        assert vuelo['Cuenta contable'] == '6290001'
        assert vuelo['Facturable'] == 'Sí'
        assert vuelo['Fecha'].date() == date(2026, 12, 1)
        assert vuelo['Importe'] == 180 and vuelo['Moneda'] == 'EUR'
        assert vuelo['Referencia'] == viaje.referencia
        assert vuelo['Persona'] == 'Todo el viaje'
        assert nombre.endswith('.xlsx')
        assert resumen == {'viajes': 1, 'gastos': 2, 'con_importes': True}

    def test_lo_que_no_tiene_importe_no_es_un_gasto(self, gestor, viaje):
        hojas, _, _ = _hojas(gestor)

        assert 'Dietas sin valorar' not in [f['Concepto'] for f in hojas['Gastos']]

    def test_un_tipo_sin_cuenta_sale_con_la_cuenta_vacia(self, gestor, viaje):
        hojas, _, _ = _hojas(gestor)

        hotel = next(f for f in hojas['Gastos']
                     if f['Tipo de gasto'] == 'Hotel / Alojamiento')
        assert hotel['Cuenta contable'] is None

    def test_la_hoja_de_viajes(self, gestor, viajero, viaje):
        hojas, _, _ = _hojas(gestor)

        (fila,) = hojas['Viajes']
        assert fila['Coste según itinerario'] == 500
        assert fila['Personas viajeras'] == viajero.nombre_completo
        assert fila['Validación'] == 'Sin pedir'

    def test_el_periodo_filtra_por_el_inicio_del_viaje(self, gestor, viaje):
        dentro, _, _ = _hojas(gestor, desde=date(2026, 12, 1), hasta=date(2026, 12, 31))
        fuera, _, _ = _hojas(gestor, desde=date(2027, 1, 1))

        assert len(dentro['Viajes']) == 1
        assert fuera['Viajes'] == [] and fuera['Gastos'] == []


@pytest.mark.security
class TestLoQueNoDebePasar:
    def test_una_formula_en_un_nombre_no_se_ejecuta(self, gestor, viaje):
        """It came out of a booking document and would run on somebody's desk."""
        contenido, _, _ = export_service.exportar(gestor)
        hoja = load_workbook(io.BytesIO(contenido))['Gastos']

        celdas = [c for fila in hoja.iter_rows(min_row=2) for c in fila
                  if isinstance(c.value, str) and 'HYPERLINK' in c.value]
        assert celdas
        assert all(c.data_type == 's' and c.value.startswith("'") for c in celdas)

    def test_sin_costes_no_hay_columnas_de_importe_y_se_dice(self, gestor, viaje):
        settings_service.set_value('COSTES_HABILITADOS', False)

        hojas, _, resumen = _hojas(gestor)

        assert 'Importe' not in hojas['Gastos'][0]
        assert 'Coste estimado' not in hojas['Viajes'][0]
        assert resumen['con_importes'] is False
        assert any('desactivados' in str(f['Valor']) for f in hojas['Acerca de'])

    def test_solo_lo_que_su_autor_puede_ver(self, gestor, viajero, viaje):
        """A traveller's unsent draft does not exist for a manager."""
        settings_service.set_value('USUARIOS_PLANIFICAN', True)
        settings_service.set_value('GESTOR_POR_DEFECTO', gestor.username)
        trip_service.create_trip(viajero, titulo='Borrador sin enviar',
                                 inicio_local=datetime(2026, 12, 10))

        hojas, _, _ = _hojas(gestor)

        assert [f['Viaje'] for f in hojas['Viajes']] == ['Auditoría en Lyon']

    def test_solo_quien_gestiona(self, as_user, admin, gestor, viajero, viaje):
        with as_user(viajero) as client:
            assert client.get('/dashboard/informes/exportar').status_code == 403
            assert 'Exportar a Excel' not in client.get(
                '/dashboard/informes').get_data(as_text=True)

        for quien in (gestor, admin):
            with as_user(quien) as client:
                respuesta = client.get('/dashboard/informes/exportar?desde=2026-12-01')
                assert respuesta.status_code == 200
                assert respuesta.mimetype == export_service.MIME
                assert 'attachment' in respuesta.headers['Content-Disposition']
                assert load_workbook(io.BytesIO(respuesta.data)).sheetnames == \
                    ['Gastos', 'Viajes', 'Acerca de']

    def test_queda_en_la_auditoria(self, gestor, viaje):
        from app.models.audit import AuditEvent

        export_service.exportar(gestor)

        assert AuditEvent.query.filter_by(accion='trips.exported').count() == 1

    def test_la_pantalla_no_deja_el_velo_puesto(self, as_user, gestor, viaje):
        """The answer is a file: the page never reloads to lift it."""
        with as_user(gestor) as client:
            html = client.get('/dashboard/informes').get_data(as_text=True)

        assert 'Exportar a Excel' in html
        formulario = html[html.index('informes/exportar'):][:200]
        assert 'data-no-loading' in formulario
