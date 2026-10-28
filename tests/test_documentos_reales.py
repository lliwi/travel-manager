"""What real documents did that the synthetic ones never had.

Each case here is a failure found by running a batch of real travel documents
through the pipeline. The texts are invented, with the shape of the originals
and none of their data.
"""
import io

import pytest

from app.services import ai_service, ocr_service
from app.services.classification_service import classify_by_rules, es_tarjeta_de_embarque
from app.services.normalization_service import _parse_datetime

pytestmark = pytest.mark.unit


def _pdf_cifrado(contrasena=''):
    """A PDF that restricts copying, opened with ``contrasena``."""
    from pypdf import PdfReader, PdfWriter

    from tests.conftest import _booking_pdf_bytes

    escritor = PdfWriter()
    for pagina in PdfReader(io.BytesIO(_booking_pdf_bytes())).pages:
        escritor.add_page(pagina)
    escritor.encrypt(user_password=contrasena, owner_password='propietario')
    salida = io.BytesIO()
    escritor.write(salida)
    return salida.getvalue()


class TestUnPdfCifrado:
    """Every sale stub from the agency arrives encrypted, with no password to
    open it: only printing and copying are restricted."""

    def _validar(self, gestor, trip, datos):
        from werkzeug.datastructures import FileStorage

        from app.extensions import db
        from app.services import document_service
        from app.tasks import document_tasks

        document = document_service.upload(
            gestor, trip, FileStorage(stream=io.BytesIO(datos), filename='talon.pdf',
                                      content_type='application/pdf'),
            commit=False,
        )
        db.session.commit()
        try:
            document_tasks.validate_document.run(str(document.id))
        except Exception:
            pass
        db.session.refresh(document)
        return document

    def test_sin_contrasena_para_abrirlo_se_procesa(self, gestor, trip):
        from app.models.enums import DocumentProcessState

        document = self._validar(gestor, trip, _pdf_cifrado(''))

        assert document.estado_proceso is DocumentProcessState.VALIDADO
        assert 'cifrado_sin_contrasena' in document.datos_validacion['avisos']

    def test_con_contrasena_se_rechaza_y_lo_dice(self, gestor, trip):
        from app.models.enums import DocumentProcessState

        document = self._validar(gestor, trip, _pdf_cifrado('secreta'))

        assert document.estado_proceso is DocumentProcessState.RECHAZADO
        # A rejection keeps its reason in the transition, as every one does.
        assert 'contraseña' in document.transitions[-1].motivo


class TestUnItinerarioNoEsUnaTarjetaDeEmbarque:
    def test_el_consejo_de_descargarla_no_la_convierte_en_una(self):
        texto = ('Itinerario de Viaje\nDestino: Barcelona\nNúmero de reserva: ABC123\n'
                 + 'Información sobre el vuelo FR9112 Turín - Barcelona\n' * 12
                 + 'Debes descargarte la aplicación para acceder a tu tarjeta de '
                   'embarque digital.')

        assert not es_tarjeta_de_embarque(texto, 'Itinerario de Viaje.eml')

    def test_pero_si_es_el_titulo_si(self):
        assert es_tarjeta_de_embarque('IBERIA - TARJETA DE EMBARQUE\nVuelo IB3210')


class TestLasFechasQueTraenLosDocumentos:
    @pytest.mark.parametrize('crudo, esperado', [
        ('03/08/26', '2026-08-03T00:00'),
        ('9 November 2026 11:30', '2026-11-09T11:30'),
        ('Miércoles 10/06/2026 15:00', '2026-06-10T15:00'),
        ('20 de octubre de 2026 17:55', '2026-10-20T17:55'),
        ('13 novembre 2026', '2026-11-13T00:00'),
        ('07/08/2026T00:00', '2026-08-07T00:00'),
    ])
    def test_se_entienden(self, crudo, esperado):
        assert _parse_datetime(crudo).isoformat(timespec='minutes') == esperado

    @pytest.mark.parametrize('crudo', ['11:55', 'Miércoles 6', '15:00'])
    def test_media_fecha_no_se_completa_a_ojo(self, crudo):
        assert _parse_datetime(crudo) is None


class TestLoQueElModeloSeInventa:
    def _datos(self, campos):
        return {'servicios': [{'campos': campos}]}

    def _paginas(self, texto):
        return [(1, texto)]

    def test_un_localizador_que_no_esta_en_el_documento_se_descarta(self):
        datos = self._datos({'localizador': 'PARKHOTELALTESKAFFEEHAUS'})

        ai_service._descartar_lo_inventado(
            datos, datos['servicios'][0]['campos'],
            self._paginas('Park Hotel Altes Kaffeehaus. Reserva 4417283'))

        assert datos['servicios'][0]['campos']['localizador'] is None
        assert any('descartado el localizador' in a for a in datos['avisos'])

    def test_el_numero_de_vuelo_no_es_un_localizador(self):
        campos = {'localizador': 'VY6514', 'numero_vuelo': 'VY6514'}
        datos = self._datos(campos)

        ai_service._descartar_lo_inventado(datos, campos,
                                           self._paginas('Vuelo VY6514 BCN-TRN'))

        assert campos['localizador'] is None

    def test_el_que_si_aparece_se_queda(self):
        campos = {'localizador': 'juufrl'}

        ai_service._descartar_lo_inventado(self._datos(campos), campos,
                                           self._paginas('Número de reserva:\nJUUFRL'))

        assert campos['localizador'] == 'juufrl'

    def test_un_importe_cero_que_el_documento_no_da_es_nulo(self):
        campos = {'importe': 0}

        ai_service._descartar_lo_inventado(self._datos(campos), campos,
                                           self._paginas('Bono de hotel. Desayuno incluido.'))

        assert campos['importe'] is None

    def test_un_cero_escrito_se_respeta(self):
        campos = {'importe': 0}

        ai_service._descartar_lo_inventado(self._datos(campos), campos,
                                           self._paginas('Cargos adicionales 0,00 EUR'))

        assert campos['importe'] == 0


class TestOtrosIdiomas:
    def test_una_fattura_de_soggiorno_es_un_hotel(self):
        from app.models.enums import DocumentClassification

        clase, confianza = classify_by_rules(
            'Fattura\nSoggiorno Dal 03/08/26 Al 07/08/26 Mueller 4 380,00 €\nOspite: Mueller',
            'F.245.pdf')

        assert clase is DocumentClassification.HOTEL
        assert confianza >= 0.7


class TestUnCorreoSinRuido:
    def test_los_enlaces_de_seguimiento_no_llegan_al_modelo(self):
        from email.message import EmailMessage

        correo = EmailMessage()
        correo['Subject'] = 'Itinerario'
        correo.set_content(
            'Número de reserva: ABC123 <https://www.ryanair.com/trip/manage/BookingId:1?utm=x>\n'
            + '<https://www.ryanair.com/content/dam/logo.png>\n' * 50
            + 'Hora de salida - 17:55')

        texto = ocr_service.extract(io.BytesIO(correo.as_bytes()),
                                    'message/rfc822').contenido

        assert 'https://' not in texto
        assert 'ABC123' in texto and '17:55' in texto


class TestElTalonReal:
    def test_el_nombre_no_se_lleva_la_columna_de_al_lado(self):
        from app.services import talon_service

        texto = ('TALÓN DE VENTA\nExpediente / Expedient Fecha / Data\n'
                 '00010/26/136612 20-10-2026 00010-588040 79032843\n'
                 'Viajero / Viatger ANA PRUEBA LOPEZ Cod. Viajero\nCodi Viatger\n'
                 'RELACIÓN DE SERVICIOS / RELACIÓ DE SERVEIS\n'
                 '01768973 HTL: HOTEL PRUEBA 100,00 RE 100,00\nMUNICH\nDUI/SA 27/10 - 29/10\n'
                 'FORMA DE PAGO / FORMA DE PAGAMENT\n')

        assert talon_service.leer(texto)['viajero'] == 'ANA PRUEBA LOPEZ'


class TestTurinEstaEnElCatalogo:
    def test_trn(self, app, seeded):
        from app.models.catalog import Location

        turin = Location.query.filter_by(codigo='TRN').one()
        assert turin.zona_horaria == 'Europe/Rome'


class TestUnaHoraSinDia:
    """A small model copies «17:55» and loses the date printed just above it."""

    def _completar(self, campos, texto):
        datos = {'servicios': [{'campos': campos}]}
        completadas = ai_service._completar_horas(datos, campos, texto)
        return completadas, datos.get('avisos', [])

    def test_se_completa_con_la_fecha_que_la_precede(self):
        campos = {'salida': {'local': '17:55'}, 'llegada': {'local': '19:25'}}

        completadas, avisos = self._completar(
            campos, 'Turín - Barcelona\n20/10/2026\nHora de salida - 17:55\n'
                    'Hora de llegada - 19:25')

        assert campos['salida']['local'] == '2026-10-20T17:55'
        assert campos['llegada']['local'] == '2026-10-20T19:25'
        assert completadas == {'salida', 'llegada'}
        assert avisos

    def test_en_un_bono_de_hotel(self):
        campos = {'check_in': {'local': '15:00'}, 'check_out': {'local': '11:00'}}

        self._completar(campos, 'Entrada: Miércoles 10/06/2026 Check-in: 15:00\n'
                                'Salida: Viernes 12/06/2026 Check-out: 11:00')

        assert campos['check_in']['local'] == '2026-06-10T15:00'
        assert campos['check_out']['local'] == '2026-06-12T11:00'

    def test_si_la_hora_apunta_a_dos_dias_no_se_elige(self):
        campos = {'salida': {'local': '10:00'}}

        self._completar(campos, 'Ida 01/11/2026 salida 10:00. Vuelta 05/11/2026 salida 10:00')

        assert campos['salida']['local'] == '10:00'

    def test_la_confianza_queda_para_revisar(self):
        bloques = [type('B', (), {'pagina': 1,
                                   'contenido': '20/10/2026\nHora de salida - 17:55'})()]
        datos = {'servicios': [{'campos': {'salida': {'local': '17:55'}}}]}

        ai_service._ground_in_document(datos, bloques)

        assert datos['servicios'][0]['confianzas']['salida'] <= ai_service.CONFIANZA_COMPLETADA


class TestUnImporteQueNoEstaEscrito:
    def test_dos_numeros_pegados_se_descartan(self):
        campos = {'importe': 2357.3949}
        datos = {'servicios': [{'campos': campos}]}

        ai_service._descartar_lo_inventado(
            datos, campos, [(1, 'Visa que termina en: 2357 39.49 EUR')])

        assert campos['importe'] is None

    @pytest.mark.parametrize('valor, texto', [
        (130.5, 'Total 130,50 EUR'), (39.49, '39.49 EUR'), (1250.0, '1.250,00 €'),
        (380.0, 'Totale 380,00 €'),
    ])
    def test_el_que_esta_escrito_se_queda(self, valor, texto):
        campos = {'importe': valor}

        ai_service._descartar_lo_inventado({'servicios': [{'campos': campos}]}, campos,
                                           [(1, texto)])

        assert campos['importe'] == valor


class TestElItinerarioQueVendeEmbarquePrioritario:
    def test_no_es_una_tarjeta(self):
        texto = ('Itinerario de Viaje Ryanair\n' + 'Información sobre el vuelo\n' * 30
                 + 'Añade Embarque prioritario a tu reserva.\n'
                 'Descarga la app para acceder a tu tarjeta de embarque digital.')

        assert not es_tarjeta_de_embarque(texto, 'Itinerario.eml')


class TestUnaFechaSinAno:
    def test_el_dia_de_la_semana_dice_el_ano(self, monkeypatch):
        from datetime import datetime

        from app.utils import timeutil

        monkeypatch.setattr(timeutil, 'utcnow', lambda: datetime(2026, 10, 1))
        campos = {'salida': {'local': '11:55'}, 'llegada': {'local': '13:30'}}

        ai_service._completar_horas({}, campos, '> BARCELONA-TURÍN lunes, 19 octubre\n11:55 13:30')

        # 19 October is a Monday in 2026, and not in 2025 or 2027.
        assert campos['salida']['local'] == '2026-10-19T11:55'
        assert campos['llegada']['local'] == '2026-10-19T13:30'

    def test_sin_dia_de_la_semana_no_se_adivina(self):
        campos = {'salida': {'local': '11:55'}}

        ai_service._completar_horas({}, campos, 'BARCELONA-TURÍN 19 octubre\n11:55')

        assert campos['salida']['local'] == '11:55'


class TestLoQueNoCuadra:
    def test_una_salida_anterior_a_la_entrada_queda_para_revisar(self, app):
        from app.services.normalization_service import normalize

        resultado = normalize({'servicios': [{'campos': {
            'check_in': {'local': '2026-10-28T14:00', 'zona_horaria': 'Europe/Berlin'},
            'check_out': {'local': '2026-10-16T14:00', 'zona_horaria': 'Europe/Berlin'},
        }, 'confianzas': {'check_in': 0.85, 'check_out': 0.85}}]}, 'hotel')

        assert any('anterior' in a for a in resultado.avisos)
        assert resultado.servicios[0]['confianzas']['check_out'] < 0.85

    def test_un_codigo_postal_no_es_una_ciudad(self, app):
        from app.services.normalization_service import normalize

        resultado = normalize({'servicios': [{'campos': {'ciudad': '10024'}}]}, 'hotel')

        assert resultado.servicios[0]['campos']['ciudad'] is None


class TestUnNuloEscrito:
    def test_el_texto_null_es_un_campo_vacio(self, app):
        from app.services.normalization_service import normalize

        resultado = normalize({'servicios': [{'campos': {
            'terminal_origen': 'null', 'terminal_destino': 'N/A', 'numero_vuelo': 'FR9112',
        }}]}, 'vuelo')

        campos = resultado.servicios[0]['campos']
        assert campos['terminal_origen'] is None and campos['terminal_destino'] is None
        assert campos['numero_vuelo'] == 'FR9112'
