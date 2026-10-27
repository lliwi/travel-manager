"""Outlook .msg messages.

A booking forwarded from Outlook is saved as .msg far more often than as .eml.
It is a compound file of MAPI properties rather than MIME, and its header is
shared with every Word 97 .doc -- so the content, not the name, has to say
that it is a message.
"""
import io
import struct
from datetime import UTC, datetime

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import DocumentProcessState, DocumentType
from app.services import document_service, ocr_service

S = DocumentProcessState

_LIBRE = 0xFFFFFFFF
_FIN_DE_CADENA = 0xFFFFFFFE
_SECTOR_FAT = 0xFFFFFFFD


def _compuesto(flujos):
    """A minimal compound file (CFB v3) holding ``flujos``.

    ``flujos`` maps ``'almacen/flujo'`` or ``'flujo'`` to bytes. Every stream
    is under the 4096-byte cutoff, so all of them live in the mini stream.
    """
    entradas = [{'nombre': 'Root Entry', 'tipo': 5, 'hijos': []}]
    almacenes = {}
    for ruta in flujos:
        if '/' in ruta:
            almacen = ruta.split('/')[0]
            if almacen not in almacenes:
                almacenes[almacen] = len(entradas)
                entradas.append({'nombre': almacen, 'tipo': 1, 'hijos': []})
                entradas[0]['hijos'].append(almacenes[almacen])

    mini = b''
    for ruta, datos in flujos.items():
        padre = almacenes[ruta.split('/')[0]] if '/' in ruta else 0
        entradas[padre]['hijos'].append(len(entradas))
        entradas.append({
            'nombre': ruta.split('/')[-1], 'tipo': 2, 'hijos': [],
            'inicio': len(mini) // 64, 'tamano': len(datos),
        })
        mini += datos + b'\x00' * (-len(datos) % 64)

    # Siblings chained down the right pointer: a degenerate tree, still valid.
    hermano_derecho = {}
    for entrada in entradas:
        for actual, siguiente in zip(entrada['hijos'], entrada['hijos'][1:], strict=False):
            hermano_derecho[actual] = siguiente

    sectores_dir = -(-len(entradas) // 4)
    sectores_mini = -(-len(mini) // 512)
    minisectores = len(mini) // 64
    sectores_minifat = -(-minisectores * 4 // 512)
    primer_dir, primer_mini = 1, 1 + sectores_dir
    primer_minifat = primer_mini + sectores_mini
    total = primer_minifat + sectores_minifat
    assert total <= 128, 'un solo sector FAT basta para este tamaño'

    fat = [_LIBRE] * 128
    fat[0] = _SECTOR_FAT
    for inicio, cuantos in ((primer_dir, sectores_dir), (primer_mini, sectores_mini),
                            (primer_minifat, sectores_minifat)):
        for i in range(cuantos):
            fat[inicio + i] = inicio + i + 1 if i < cuantos - 1 else _FIN_DE_CADENA
    minifat = [i + 1 for i in range(minisectores)]
    # Each stream's mini chain ends at its own last minisector.
    for entrada in entradas:
        if entrada['tipo'] == 2:
            ultimo = entrada['inicio'] + -(-entrada['tamano'] // 64) - 1
            minifat[ultimo] = _FIN_DE_CADENA
    minifat += [_LIBRE] * (sectores_minifat * 128 - len(minifat))

    directorio = b''
    for indice, entrada in enumerate(entradas):
        nombre = entrada['nombre'].encode('utf-16-le') + b'\x00\x00'
        if entrada['tipo'] == 5:
            inicio, tamano = primer_mini, len(mini)
        elif entrada['tipo'] == 2:
            inicio, tamano = entrada['inicio'], entrada['tamano']
        else:
            inicio, tamano = 0, 0
        directorio += struct.pack(
            '<64sHBBIII16sIQQIQ',
            nombre, len(nombre), entrada['tipo'], 1,
            _LIBRE, hermano_derecho.get(indice, _LIBRE),
            entrada['hijos'][0] if entrada['hijos'] else _LIBRE,
            b'\x00' * 16, 0, 0, 0, inicio, tamano,
        )
    vacia = struct.pack('<64sHBBIII16sIQQIQ', b'', 0, 0, 0, _LIBRE, _LIBRE,
                        _LIBRE, b'\x00' * 16, 0, 0, 0, 0, 0)
    directorio += vacia * (sectores_dir * 4 - len(entradas))

    cabecera = struct.pack(
        '<8s16sHHHHH6sIIIIIIIII',
        ocr_service.CABECERA_OLE, b'\x00' * 16, 0x3E, 3, 0xFFFE, 9, 6, b'\x00' * 6,
        0, 1, primer_dir, 0, 4096, primer_minifat, sectores_minifat,
        _FIN_DE_CADENA, 0,
    ) + struct.pack('<109I', 0, *([_LIBRE] * 108))

    return (
        cabecera
        + struct.pack('<128I', *fat)
        + directorio
        + mini + b'\x00' * (-len(mini) % 512)
        + struct.pack(f'<{len(minifat)}I', *minifat)
    )


def _texto(propiedad, valor, almacen=None):
    nombre = f'__substg1.0_{propiedad:04X}001F'
    return {f'{almacen}/{nombre}' if almacen else nombre: valor.encode('utf-16-le')}


def _fecha(propiedad, instante):
    filetime = int((instante - datetime(1601, 1, 1, tzinfo=UTC))
                   .total_seconds() * 10_000_000)
    entrada = struct.pack('<IIQ', (propiedad << 16) | 0x0040, 6, filetime)
    return {'__properties_version1.0': b'\x00' * 32 + entrada}


def _mensaje(*partes, clase='IPM.Note'):
    flujos = {}
    if clase:
        flujos.update(_texto(0x001A, clase))
    for parte in partes:
        flujos.update(parte)
    return _compuesto(flujos)


def _confirmacion():
    return _mensaje(
        _texto(0x0037, 'Confirmación de reserva ODIVYR'),
        _texto(0x0C1A, 'Vueling'),
        _texto(0x5D01, 'reservas@vueling.com'),
        _texto(0x0E04, 'Ana Pérez'),
        _texto(0x1000, 'Vuelo VY7622 BCN-LGW 31/10/2026 19:55h'),
        _fecha(0x0039, datetime(2026, 10, 1, 9, 30, tzinfo=UTC)),
        _texto(0x3707, 'tarjeta_embarque.pdf', '__attach_version1.0_#00000000'),
    )


def _upload(gestor, trip, data, filename='reserva.msg'):
    storage = FileStorage(
        stream=io.BytesIO(data), filename=filename,
        content_type='application/vnd.ms-outlook',
    )
    document = document_service.upload(
        gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False
    )
    db.session.commit()
    return document


@pytest.mark.unit
class TestValidacion:
    def test_un_msg_de_outlook_pasa_la_validacion(self, gestor, trip):
        from app.tasks import document_tasks

        document = _upload(gestor, trip, _confirmacion())
        document_tasks.validate_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.VALIDADO
        assert document.mime == ocr_service.MIME_OUTLOOK

    def test_un_compuesto_que_no_es_correo_se_rechaza(self, gestor, trip):
        """A .doc shares the header; renamed to .msg it is still a .doc."""
        from app.tasks import document_tasks
        from app.utils.errors import PermanentError

        documento_word = _compuesto({'WordDocument': b'\xec\xa5' + b'\x00' * 200})
        document = _upload(gestor, trip, documento_word)

        with pytest.raises(PermanentError):
            document_tasks.validate_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.RECHAZADO


@pytest.mark.unit
class TestExtraccion:
    def _extraer(self, datos):
        return ocr_service.extract(io.BytesIO(datos), ocr_service.MIME_OUTLOOK)

    def test_se_leen_cabeceras_cuerpo_y_adjuntos(self):
        texto = self._extraer(_confirmacion()).pages[0].contenido

        assert 'From: Vueling <reservas@vueling.com>' in texto
        assert 'To: Ana Pérez' in texto
        assert 'Subject: Confirmación de reserva ODIVYR' in texto
        assert 'Date: Thu, 01 Oct 2026 09:30:00 +0000' in texto
        assert 'VY7622 BCN-LGW 31/10/2026 19:55h' in texto
        assert 'Adjuntos: tarjeta_embarque.pdf' in texto

    def test_sin_texto_plano_se_aplana_el_html(self):
        html = '<html><body><p>Hotel Arts</p><p>Entrada 31/10/2026</p></body></html>'
        datos = _mensaje({'__substg1.0_10130102': html.encode('utf-8')})

        texto = self._extraer(datos).pages[0].contenido

        assert 'Hotel Arts' in texto and 'Entrada 31/10/2026' in texto
        assert '<p>' not in texto

    def test_una_direccion_de_exchange_no_se_muestra(self):
        datos = _mensaje(
            _texto(0x0C1A, 'Agencia'),
            _texto(0x0C1F, '/O=EXCHANGELABS/OU=EXCHANGE/CN=RECIPIENTS/CN=AGENCIA'),
        )

        texto = self._extraer(datos).pages[0].contenido

        assert 'From: Agencia' in texto
        assert 'EXCHANGELABS' not in texto

    def test_un_msg_danado_es_un_error_permanente(self):
        from app.utils.errors import PermanentError

        with pytest.raises(PermanentError):
            self._extraer(ocr_service.CABECERA_OLE + b'\x00' * 2000)
