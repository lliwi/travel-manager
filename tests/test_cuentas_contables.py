"""Which ledger account each kind of travel expense is posted to.

Two things fail quietly here: an expense reported under an account nobody
chose, and a chart of accounts that anybody but an administrator can change.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import TipoGasto, TripStatus
from app.models.settings import CuentaContable
from app.services import (
    cuenta_contable_service,
    itinerary_service,
    report_service,
    settings_service,
    trip_service,
)
from app.utils.errors import ValidationError
from app.utils.timeutil import utcnow


@pytest.fixture
def viaje(gestor):
    settings_service.set_value('COSTES_HABILITADOS', True)
    hoy = utcnow().replace(tzinfo=None)
    trip = trip_service.create_trip(
        gestor, titulo='Auditoría en Lyon', estado=TripStatus.BORRADOR,
        inicio_local=hoy, fin_local=hoy, inicio_tz='Europe/Madrid',
    )
    itinerary_service.create_item(
        gestor, trip, 'segmento',
        instants={'salida': (datetime(2026, 12, 1, 7, 10), 'Europe/Madrid'),
                  'llegada': (datetime(2026, 12, 1, 9, 0), 'Europe/Paris')},
        origen_codigo='BCN', destino_codigo='LYS', importe=180, moneda='EUR',
    )
    itinerary_service.create_item(
        gestor, trip, 'alojamiento', nombre='Hotel Carlton',
        instants={'check_in': (datetime(2026, 12, 1, 15, 0), 'Europe/Paris'),
                  'check_out': (datetime(2026, 12, 3, 11, 0), 'Europe/Paris')},
        importe=320, moneda='EUR',
    )
    itinerary_service.create_item(
        gestor, trip, 'servicio', tipo='dietas', nombre='Dietas de tres días',
        importe=150, moneda='EUR',
    )
    return trip


class TestLaTabla:
    def test_ofrece_todos_los_tipos_aunque_no_tengan_cuenta(self, seeded):
        tabla = cuenta_contable_service.tabla()

        assert [tipo for tipo, _ in tabla] == list(TipoGasto)
        assert all(fila is None for _, fila in tabla)

    def test_guarda_cambia_y_quita(self, admin):
        cuenta_contable_service.guardar(admin, {
            'vuelo': ('6290001', 'Desplazamientos'), 'alojamiento': ('6290002', ''),
        })
        cuenta_contable_service.guardar(admin, {
            'vuelo': ('6290009', 'Billetes de avión'), 'alojamiento': ('', ''),
        })

        (fila,) = CuentaContable.query.all()
        assert (fila.tipo_gasto, fila.cuenta, fila.descripcion) == \
            (TipoGasto.VUELO, '6290009', 'Billetes de avión')

    def test_lo_que_no_se_envia_no_se_toca(self, admin):
        cuenta_contable_service.guardar(admin, {'vuelo': ('6290001', '')})
        cuenta_contable_service.guardar(admin, {'dietas': ('6290003', '')})

        assert {f.tipo_gasto for f in CuentaContable.query.all()} == \
            {TipoGasto.VUELO, TipoGasto.DIETAS}

    def test_un_valor_malo_no_guarda_nada(self, admin):
        with pytest.raises(ValidationError):
            cuenta_contable_service.guardar(admin, {
                'vuelo': ('6290001', ''), 'alojamiento': ('', 'Sin cuenta'),
            })
        db.session.rollback()

        assert not CuentaContable.query.count()

    def test_queda_en_la_auditoria(self, admin):
        from app.models.audit import AuditEvent

        cuenta_contable_service.guardar(admin, {'vuelo': ('6290001', '')})

        assert AuditEvent.query.filter_by(accion='accounts.updated').count() == 1


class TestLaPantalla:
    def test_el_administrador_la_guarda(self, as_user, admin):
        with as_user(admin) as client:
            html = client.get('/admin/cuentas-contables').get_data(as_text=True)
            respuesta = client.post('/admin/cuentas-contables', data={
                'cuenta__vuelo': '6290001', 'descripcion__vuelo': 'Desplazamientos',
                'cuenta__dietas': '6290003', 'descripcion__dietas': '',
            })

        assert 'Dietas' in html and 'Hotel / Alojamiento' in html
        assert respuesta.status_code == 302
        assert cuenta_contable_service.por_tipo()[TipoGasto.DIETAS].cuenta == '6290003'

    @pytest.mark.security
    def test_nadie_mas(self, as_user, gestor, viajero):
        for usuario in (gestor, viajero):
            with as_user(usuario) as client:
                assert client.get('/admin/cuentas-contables').status_code == 403
                assert client.post('/admin/cuentas-contables',
                                   data={'cuenta__vuelo': '1'}).status_code == 403
        assert not CuentaContable.query.count()


class TestElInforme:
    def test_agrupa_por_tipo_con_su_cuenta(self, admin, gestor, viaje):
        cuenta_contable_service.guardar(admin, {
            'vuelo': ('6290001', 'Desplazamientos'), 'dietas': ('6290003', 'Dietas'),
        })

        informe = report_service.gastos_por_cuenta(gestor)
        filas = {fila['tipo']: fila for fila in informe['cuentas']}

        assert filas['Vuelo']['cuenta'] == '6290001'
        assert filas['Vuelo']['total'] == 180
        assert filas['Dietas']['total'] == 150
        assert informe['total'] == 650
        assert informe['moneda'] == 'EUR'

    def test_lo_que_no_tiene_cuenta_se_dice_y_va_al_final(self, admin, gestor, viaje):
        cuenta_contable_service.guardar(admin, {'dietas': ('6290003', '')})

        informe = report_service.gastos_por_cuenta(gestor)

        assert [f['cuenta'] for f in informe['cuentas']] == ['6290003', None, None]
        assert informe['sin_cuenta'] == 2

    @pytest.mark.security
    def test_quien_no_ve_costes_no_lo_recibe(self, viajero, gestor, viaje):
        trip_service.add_traveler(gestor, viaje, viajero)

        assert report_service.gastos_por_cuenta(viajero) is None

    def test_sale_en_la_pagina_de_informes(self, as_user, admin, gestor, viaje):
        cuenta_contable_service.guardar(admin, {'vuelo': ('6290001', 'Desplazamientos')})

        with as_user(gestor) as client:
            html = client.get('/dashboard/informes').get_data(as_text=True)

        assert 'Gasto por cuenta contable y mes' in html
        assert '6290001' in html
        assert 'Sin cuenta asignada' in html
