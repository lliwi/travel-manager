"""Text extraction and OCR.

Specification section 2.3 step 2 and section 6: a local engine is preferred,
behind an interface that a permitted external service could replace.

Text is taken from a PDF's text layer when there is one, and OCR is used only
when there is not -- OCR is slow and lossy, so running it on a digitally
generated booking PDF would make extraction worse, not better.
"""
import io
import logging
import re
import struct
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from flask import current_app

from app.utils.errors import PermanentError, ProcessingError

logger = logging.getLogger(__name__)

MIME_OUTLOOK = 'application/vnd.ms-outlook'

#: Every compound file starts like this: an Outlook .msg, but also a Word 97
#: .doc or an Excel .xls, so the header alone does not say which.
CABECERA_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

#: An .xlsx is a ZIP, and so are a .docx, a .jar and an archive of anything.
CABECERA_ZIP = b'PK\x03\x04'

#: What a workbook may expand to. A few hundred kilobytes of ZIP can inflate
#: to gigabytes, and the worker that opens it is the one that falls over.
XLSX_MAX_DESCOMPRIMIDO = 64 * 1024 * 1024

#: How much of each sheet is read. A request form fits in a corner of the
#: first sheet; a sheet formatted down to row a million would otherwise be
#: walked cell by cell.
XLSX_MAX_HOJAS = 20
XLSX_MAX_FILAS = 500
XLSX_MAX_COLUMNAS = 40


class PageText:
    """The text of one page."""

    __slots__ = ('numero', 'contenido', 'uso_ocr', 'confianza')

    def __init__(self, numero, contenido, uso_ocr=False, confianza=None):
        self.numero = numero
        self.contenido = contenido or ''
        self.uso_ocr = uso_ocr
        self.confianza = confianza

    @property
    def caracteres(self):
        return len(self.contenido)


class ExtractedText:
    """The text of a whole document."""

    def __init__(self, pages, motor, uso_ocr=False, idioma=None):
        self.pages = pages
        self.motor = motor
        self.uso_ocr = uso_ocr
        self.idioma = idioma

    @property
    def contenido(self):
        return '\n\n'.join(p.contenido for p in self.pages if p.contenido)

    @property
    def caracteres(self):
        return sum(p.caracteres for p in self.pages)

    @property
    def tiene_texto(self):
        return self.caracteres > 0

    def __len__(self):
        return len(self.pages)


def extract(stream, mime, filename=None):
    """Extract text from an uploaded file.

    Args:
        stream: File-like object positioned anywhere; it is rewound first.
        mime: The detected MIME type.
        filename: Original name, used only for logging.

    Returns:
        An :class:`ExtractedText`.
    """
    if hasattr(stream, 'seek'):
        stream.seek(0)

    if mime == 'application/pdf':
        return _extract_pdf(stream)
    if mime and mime.startswith('image/'):
        return _extract_image(stream)
    if mime in ('message/rfc822', 'text/plain'):
        return _extract_email(stream)
    if mime == MIME_OUTLOOK:
        return _extract_outlook(stream)
    if mime == MIME_XLSX:
        return _extract_xlsx(stream)

    raise PermanentError(f'No se sabe extraer texto de un archivo {mime}.')


def _extract_pdf(stream):
    """Read a PDF's text layer, falling back to OCR page by page."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ProcessingError('pdfplumber no está instalado.') from exc

    min_chars = current_app.config['OCR_MIN_CHARS_PER_PAGE']
    pages = []
    needs_ocr = []

    with pdfplumber.open(stream) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or '').strip()
            if len(text) < min_chars:
                needs_ocr.append(index)
            pages.append(PageText(index, text))

    # A born-digital PDF needs no OCR at all, which is the common case for
    # airline and hotel confirmations.
    if not needs_ocr:
        return ExtractedText(pages, motor='pdfplumber', uso_ocr=False,
                             idioma=_detect_language(pages))

    if not _ocr_available():
        logger.warning(
            'El PDF necesita OCR en las páginas %s pero el motor no está disponible.',
            needs_ocr,
        )
        return ExtractedText(pages, motor='pdfplumber', uso_ocr=False,
                             idioma=_detect_language(pages))

    ocr_pages = _ocr_pdf_pages(stream, needs_ocr)
    for page in pages:
        replacement = ocr_pages.get(page.numero)
        if replacement is not None and replacement.caracteres > page.caracteres:
            pages[page.numero - 1] = replacement

    return ExtractedText(
        pages, motor='pdfplumber+tesseract', uso_ocr=True,
        idioma=_detect_language(pages),
    )


def _ocr_pdf_pages(stream, page_numbers):
    """Rasterise and OCR the named pages of a PDF."""
    try:
        import pytesseract  # noqa: F401  (comprobación de disponibilidad)
        from pdf2image import convert_from_bytes
    except ImportError:
        return {}

    if hasattr(stream, 'seek'):
        stream.seek(0)
    data = stream.read()
    dpi = current_app.config['OCR_DPI']
    from app.services import settings_service

    languages = settings_service.idiomas_ocr()

    results = {}
    for number in page_numbers:
        try:
            images = convert_from_bytes(
                data, dpi=dpi, first_page=number, last_page=number
            )
            if not images:
                continue
            text = _leer(images[0], languages)
            results[number] = PageText(number, text, uso_ocr=True)
        except Exception as exc:
            logger.warning('Falló el OCR de la página %s: %s', number, exc)

    return results


def _extract_image(stream):
    """OCR a scanned image."""
    if not _ocr_available():
        raise ProcessingError(
            'El documento es una imagen y el motor de OCR no está disponible.'
        )

    from PIL import Image

    if hasattr(stream, 'seek'):
        stream.seek(0)
    image = Image.open(stream)
    from app.services import settings_service

    text = _leer(image, settings_service.idiomas_ocr())
    page = PageText(1, text, uso_ocr=True)
    return ExtractedText([page], motor='tesseract', uso_ocr=True,
                         idioma=_detect_language([page]))


#: «<https://www.ryanair.com/content/dam/...>»: where a link pointed, left
#: behind by the mail client's plain rendering of an HTML message.
_ENLACE = re.compile(r'<(?:https?|mailto):[^>\s]*>')


def _sin_enlaces(texto):
    """An email body without its link targets.

    A Ryanair itinerary is fourteen thousand characters of which most are
    tracking URLs. They tell the model nothing, cost it attention, and can
    push the flight times past the length it is sent.
    """
    limpio = _ENLACE.sub('', texto or '')
    limpio = re.sub(r'[ \t]+\n', '\n', limpio)
    return re.sub(r'\n{3,}', '\n\n', limpio).strip()


def _extract_email(stream):
    """Read the text parts of an EML message, and its readable attachments."""
    import email
    from email import policy

    if hasattr(stream, 'seek'):
        stream.seek(0)
    message = email.message_from_bytes(stream.read(), policy=policy.default)

    parts = []
    headers = []
    for header in ('From', 'To', 'Subject', 'Date'):
        value = message.get(header)
        if value:
            headers.append(f'{header}: {value}')
    if headers:
        parts.append('\n'.join(headers))

    body = message.get_body(preferencelist=('plain', 'html'))
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == 'text/html':
            content = _strip_html(content)
        parts.append(_sin_enlaces(content))

    attachments = [
        (part.get_filename(), part.get_payload(decode=True))
        for part in message.iter_attachments()
        if part.get_filename()
    ]
    if attachments:
        parts.append('Adjuntos: ' + ', '.join(nombre for nombre, _ in attachments))

    return _mensaje_con_adjuntos('\n\n'.join(p for p in parts if p), attachments,
                                 motor='email')


# MAPI property ids ([MS-OXPROPS]) read from an Outlook message.
_PR_MESSAGE_CLASS = 0x001A
_PR_SUBJECT = 0x0037
_PR_CLIENT_SUBMIT_TIME = 0x0039
_PR_SENDER_NAME = 0x0C1A
_PR_SENDER_EMAIL = 0x0C1F
_PR_DISPLAY_CC = 0x0E03
_PR_DISPLAY_TO = 0x0E04
_PR_BODY = 0x1000
_PR_BODY_HTML = 0x1013
_PR_DISPLAY_NAME = 0x3001
_PR_ATTACH_DATA = 0x3701
_PR_ATTACH_FILENAME = 0x3704
_PR_ATTACH_LONG_FILENAME = 0x3707
_PR_SENDER_SMTP_ADDRESS = 0x5D01

_PT_SYSTIME = 0x0040


def es_mensaje_outlook(stream):
    """Whether a compound file is an Outlook message and not some other one.

    Only the streams inside tell a .msg from a .doc; a message always carries
    its class, and every message class starts with ``IPM.``.
    """
    try:
        import olefile
    except ImportError as exc:
        raise ProcessingError('olefile no está instalado.') from exc

    stream.seek(0)
    try:
        with olefile.OleFileIO(stream) as ole:
            clase = _propiedad_texto(ole, _PR_MESSAGE_CLASS) or ''
            return clase.upper().startswith('IPM.')
    except Exception:  # olefile raises whatever a damaged file trips
        return False
    finally:
        stream.seek(0)


def _extract_outlook(stream):
    """Read an Outlook .msg the way :func:`_extract_email` reads an EML.

    A .msg is a compound file of MAPI properties, not MIME, so the ``email``
    package cannot open it. The text is laid out as the EML path lays it out
    -- headers, body, attachment names, then the attachments that can be read
    -- so extraction sees one shape of message whichever client exported it.
    """
    try:
        import olefile
    except ImportError as exc:
        raise ProcessingError('olefile no está instalado.') from exc

    stream.seek(0)
    try:
        ole = olefile.OleFileIO(stream)
    except Exception as exc:  # see es_mensaje_outlook
        raise PermanentError(
            'El archivo no es un mensaje de Outlook legible.'
        ) from exc
    with ole:
        texto, adjuntos = _texto_outlook(ole)

    return _mensaje_con_adjuntos(texto, adjuntos, motor='outlook')


def _texto_outlook(ole):
    parts = []
    headers = []

    remitente = _propiedad_texto(ole, _PR_SENDER_NAME)
    direccion = (_propiedad_texto(ole, _PR_SENDER_SMTP_ADDRESS)
                 or _propiedad_texto(ole, _PR_SENDER_EMAIL))
    # Inside an Exchange organisation the sender address is an X.500 path,
    # which tells the reader nothing a name does not.
    if direccion and '@' not in direccion:
        direccion = None
    if remitente and direccion and direccion != remitente:
        remitente = f'{remitente} <{direccion}>'
    remitente = remitente or direccion

    fecha = _propiedad_fecha(ole, _PR_CLIENT_SUBMIT_TIME)
    for header, value in (
        ('From', remitente),
        ('To', _propiedad_texto(ole, _PR_DISPLAY_TO)),
        ('Cc', _propiedad_texto(ole, _PR_DISPLAY_CC)),
        ('Subject', _propiedad_texto(ole, _PR_SUBJECT)),
        ('Date', fecha and format_datetime(fecha)),
    ):
        if value:
            headers.append(f'{header}: {value}')
    if headers:
        parts.append('\n'.join(headers))

    # Outlook keeps a plain rendering beside the HTML one; when it is missing
    # the HTML is flattened exactly as an HTML-only EML would be.
    cuerpo = _propiedad_texto(ole, _PR_BODY)
    if not cuerpo:
        html = _propiedad_binaria(ole, _PR_BODY_HTML)
        if html:
            cuerpo = _strip_html(_decodificar(html))
    if cuerpo:
        parts.append(_sin_enlaces(cuerpo))

    adjuntos = []
    for ruta in ole.listdir(streams=False, storages=True):
        if len(ruta) != 1 or not ruta[0].startswith('__attach_version1.0_'):
            continue
        nombre = (_propiedad_texto(ole, _PR_ATTACH_LONG_FILENAME, ruta[0])
                  or _propiedad_texto(ole, _PR_ATTACH_FILENAME, ruta[0])
                  or _propiedad_texto(ole, _PR_DISPLAY_NAME, ruta[0]))
        if nombre:
            # An embedded message keeps PR_ATTACH_DATA as a storage, not a
            # stream, and so yields no bytes here: it is named and not read.
            adjuntos.append((nombre, _propiedad_binaria(ole, _PR_ATTACH_DATA, ruta[0])))
    if adjuntos:
        parts.append('Adjuntos: ' + ', '.join(nombre for nombre, _ in adjuntos))

    return '\n\n'.join(p for p in parts if p), adjuntos


def _propiedad_texto(ole, propiedad, almacen=None):
    """A string property, Unicode (001F) or in the message's code page (001E)."""
    for tipo, codec in (('001F', 'utf-16-le'), ('001E', 'cp1252')):
        crudo = _leer_flujo(ole, f'__substg1.0_{propiedad:04X}{tipo}', almacen)
        if crudo is not None:
            return crudo.decode(codec, errors='replace').rstrip('\x00').strip()
    return None


def _propiedad_binaria(ole, propiedad, almacen=None):
    return (_leer_flujo(ole, f'__substg1.0_{propiedad:04X}0102', almacen)
            or _leer_flujo(ole, f'__substg1.0_{propiedad:04X}001F', almacen))


#: How many attachments of one message are read. A thread forwarded with its
#: whole history can carry dozens, and each PDF may need OCR.
ADJUNTOS_MAX = 5


def _mensaje_con_adjuntos(texto, adjuntos, motor):
    """The message as page 1, and each readable attachment after it.

    A booking is usually sent as a PDF attached to a two-line email, and an
    internal request as the spreadsheet attached to «te paso la solicitud».
    Reading only the body left nothing to extract, and the document failed
    with the booking sitting inside it.

    An attachment that cannot be read is said so in the text rather than
    failing the message: the body may still be what matters.
    """
    paginas = [PageText(1, texto)]
    uso_ocr = False
    for nombre, datos in [a for a in adjuntos if a[1]][:ADJUNTOS_MAX]:
        lector = _lector_de_adjunto(datos)
        if lector is None:
            continue
        try:
            leido = lector(io.BytesIO(datos))
        except Exception as exc:  # one bad attachment must not sink the message
            logger.info('No se pudo leer el adjunto %s: %s', nombre, exc)
            paginas.append(PageText(len(paginas) + 1,
                                    f'Adjunto: {nombre} (no se ha podido leer)'))
            continue
        uso_ocr = uso_ocr or leido.uso_ocr
        for pagina in leido.pages:
            paginas.append(PageText(
                len(paginas) + 1, f'Adjunto: {nombre}\n{pagina.contenido}',
                uso_ocr=pagina.uso_ocr, confianza=pagina.confianza,
            ))
    return ExtractedText(paginas, motor=motor, uso_ocr=uso_ocr,
                         idioma=_detect_language(paginas))


def _lector_de_adjunto(datos):
    """How to read an attachment, decided by its bytes like an upload is."""
    if datos.startswith(b'%PDF-'):
        return _extract_pdf
    if datos.startswith(CABECERA_ZIP):
        return lambda stream: (
            _extract_xlsx(stream) if tipo_de_zip(stream) == MIME_XLSX
            else ExtractedText([], motor='zip')
        )
    return None


def _leer_flujo(ole, nombre, almacen=None):
    ruta = f'{almacen}/{nombre}' if almacen else nombre
    if not ole.exists(ruta):
        return None
    return ole.openstream(ruta).read()


def _propiedad_fecha(ole, propiedad):
    """A PT_SYSTIME property from the message's fixed-size property stream.

    The stream opens with a 32-byte header, then one 16-byte entry per
    property: tag (type in the low word, id in the high one), flags, value.
    """
    crudo = _leer_flujo(ole, '__properties_version1.0')
    if not crudo:
        return None
    buscada = (propiedad << 16) | _PT_SYSTIME
    for inicio in range(32, len(crudo) - 15, 16):
        etiqueta, _, valor = struct.unpack_from('<IIQ', crudo, inicio)
        if etiqueta == buscada and valor:
            # FILETIME: hundreds of nanoseconds since 1601, always UTC.
            return (datetime(1601, 1, 1, tzinfo=UTC)
                    + timedelta(microseconds=valor // 10))
    return None


def _decodificar(crudo):
    try:
        return crudo.decode('utf-8')
    except UnicodeDecodeError:
        return crudo.decode('cp1252', errors='replace')


def tipo_de_zip(stream):
    """The real type of a ZIP: an Excel workbook, or just a ZIP.

    Raises :class:`PermanentError` for a workbook that must not be opened --
    one carrying macros, or one that inflates past the limit -- because those
    are refusals worth explaining, not merely an unexpected type.
    """
    import zipfile

    stream.seek(0)
    try:
        with zipfile.ZipFile(stream) as archivo:
            miembros = archivo.infolist()
    except zipfile.BadZipFile:
        return 'application/zip'
    finally:
        stream.seek(0)

    nombres = {m.filename for m in miembros}
    if 'xl/workbook.xml' not in nombres:
        return 'application/zip'
    if any(n.lower().endswith('vbaproject.bin') for n in nombres):
        raise PermanentError(
            'El libro de Excel contiene macros y no se admite. Guárdelo como '
            '.xlsx sin macros y vuelva a subirlo.'
        )
    if sum(m.file_size for m in miembros) > XLSX_MAX_DESCOMPRIMIDO:
        raise PermanentError('El libro de Excel es demasiado grande una vez descomprimido.')
    return MIME_XLSX


def _extract_xlsx(stream):
    """One page per visible sheet, one line per row, ``|`` between cells.

    Cached values, not formulas: a reviewer reading «=SUM(J34,J29)» learns
    nothing, and the request form's second sheet is mostly references to the
    first. A hidden sheet is skipped, since nobody looking at the workbook
    would see what it says.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ProcessingError('openpyxl no está instalado.') from exc

    stream.seek(0)
    try:
        libro = openpyxl.load_workbook(stream, data_only=True)
    except Exception as exc:  # openpyxl raises whatever a damaged file trips
        raise PermanentError('El libro de Excel está dañado y no puede leerse.') from exc

    pages = []
    try:
        for hoja in libro.worksheets[:XLSX_MAX_HOJAS]:
            if hoja.sheet_state != 'visible':
                continue
            lineas = [f'Hoja: {hoja.title}']
            for fila in hoja.iter_rows(max_row=XLSX_MAX_FILAS,
                                       max_col=XLSX_MAX_COLUMNAS, values_only=True):
                celdas = [_celda(v) for v in fila]
                celdas = [c for c in celdas if c]
                if celdas:
                    lineas.append(' | '.join(celdas))
            pages.append(PageText(len(pages) + 1, '\n'.join(lineas)))
    finally:
        libro.close()

    if not pages:
        raise PermanentError('El libro de Excel no tiene ninguna hoja visible.')
    return ExtractedText(pages, motor='hoja_calculo', uso_ocr=False,
                         idioma=_detect_language(pages))


def _celda(valor):
    """A cell as text, with dates in ISO so nothing downstream has to guess."""
    from datetime import date, time

    if valor is None:
        return ''
    if isinstance(valor, bool):
        return 'Sí' if valor else 'No'
    if isinstance(valor, datetime):
        if valor.time() == time(0, 0):
            return valor.date().isoformat()
        return valor.isoformat(sep=' ', timespec='minutes')
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, time):
        return valor.strftime('%H:%M')
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    # The separator is reserved for cell boundaries.
    return ' '.join(str(valor).replace('|', '/').split())


#: Page segmentation modes worth trying, in the order they tend to win.
#:
#: 6 assumes one uniform block, which a ticket is not: a boarding pass is a
#: grid of little labelled boxes, and read as a paragraph its columns interleave
#: into nonsense. 4 reads columns, 11 reads scattered text, 3 lets Tesseract
#: decide. Trying several and keeping the best costs a second per page and is
#: the difference between a locator read and a locator missed.
_MODOS_SEGMENTACION = (6, 4, 11, 3)


def _umbral_otsu(histograma):
    """The threshold that best separates ink from paper, from a histogram.

    A fixed cut at 160 assumes a clean scan on white. A photographed ticket, a
    grey fax or a dark-mode boarding pass falls entirely on one side of it and
    comes back blank. Otsu's method derives the cut from the image instead of
    assuming one.
    """
    total = sum(histograma)
    if not total:
        return 128

    suma_total = sum(i * n for i, n in enumerate(histograma))
    suma_fondo = 0.0
    peso_fondo = 0
    mejor_varianza = -1.0
    mejor = 128

    for nivel, cuenta in enumerate(histograma):
        peso_fondo += cuenta
        if peso_fondo == 0:
            continue
        peso_primer_plano = total - peso_fondo
        if peso_primer_plano == 0:
            break

        suma_fondo += nivel * cuenta
        media_fondo = suma_fondo / peso_fondo
        media_primer = (suma_total - suma_fondo) / peso_primer_plano
        varianza = peso_fondo * peso_primer_plano * (media_fondo - media_primer) ** 2

        if varianza > mejor_varianza:
            mejor_varianza = varianza
            mejor = nivel

    return mejor


def _puntuar_lectura(texto):
    """How much of a reading looks like language rather than speckle.

    Tesseract returns something for every attempt; picking the longest would
    reward the mode that turned the border of the scan into punctuation. What
    counts is alphanumeric characters that sit inside words.
    """
    if not texto:
        return 0

    palabras = [p for p in texto.split() if any(c.isalnum() for c in p)]
    utiles = sum(len(p) for p in palabras if len(p) > 1)
    ruido = sum(1 for c in texto if not c.isalnum() and not c.isspace())
    return utiles - ruido // 4


def _preprocess(image):
    """Greyscale, stretch the contrast, and threshold where the image says."""
    try:
        from PIL import ImageOps

        grey = ImageOps.autocontrast(image.convert('L'))
        umbral = _umbral_otsu(grey.histogram())
        return grey.point(lambda p: 255 if p > umbral else 0)
    except Exception:
        return image


def _enderezar(image):
    """Rotate a page that was scanned or photographed sideways.

    Best effort: orientation detection needs a trained model that may not be
    installed, and a page read upside down is worth trying to fix but not worth
    failing over.
    """
    try:
        import pytesseract

        osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT)
        giro = int(osd.get('rotate') or 0) % 360
    except Exception:
        return image

    return image.rotate(-giro, expand=True) if giro else image


def _leer(image, languages):
    """Read a page, keeping the best of several segmentation modes."""
    import pytesseract

    preparada = _preprocess(_enderezar(image))

    mejor_texto = ''
    mejor_puntuacion = 0
    for modo in _MODOS_SEGMENTACION:
        try:
            texto = pytesseract.image_to_string(
                preparada, lang=languages, config=f'--psm {modo}',
            )
        except Exception as exc:
            logger.debug('El modo psm %s falló: %s', modo, exc)
            continue

        puntuacion = _puntuar_lectura(texto)
        if puntuacion > mejor_puntuacion:
            mejor_texto, mejor_puntuacion = texto, puntuacion

        # A clean page reads well on the first mode; the rest are for the ones
        # that do not, so there is no point paying for them every time.
        if mejor_puntuacion > 400:
            break

    return mejor_texto.strip()


_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\n{3,}')
_BLOCK_RE = re.compile(r'(?i)<br\s*/?>|</(p|div|tr|table|li|h[1-6])\s*>')
_CELL_RE = re.compile(r'(?i)</(td|th)\s*>')
_SEP_RUN_RE = re.compile(r'(?:\|\s*){2,}')


def _strip_html(html):
    """Crude HTML-to-text, adequate for a booking confirmation email.

    Airlines lay booking confirmations out as tables, and a table only means
    anything if a row survives as a row: the flight number, the airport and the
    two times belong together. So a cell boundary becomes a separator and a row
    boundary becomes the newline, while the newlines the HTML source happens to
    contain between tags are discarded -- keeping them scatters each cell onto
    its own line and leaves an extractor, human or model, guessing which time
    goes with which flight.
    """
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    # A line break in HTML source is insignificant whitespace; the structure is
    # in the tags. Discarding those breaks first is what makes the rules below
    # the only thing that starts a new line.
    text = re.sub(r'\s+', ' ', text)
    text = _BLOCK_RE.sub('\n', text)
    text = _CELL_RE.sub(' | ', text)
    text = _TAG_RE.sub(' ', text)
    import html as html_module

    text = html_module.unescape(text).replace('\xa0', ' ')

    lineas = []
    for linea in text.split('\n'):
        linea = _SEP_RUN_RE.sub('| ', ' '.join(linea.split()))
        linea = linea.strip().strip('|').strip()
        lineas.append(linea)

    return _WS_RE.sub('\n\n', '\n'.join(lineas)).strip()


def _ocr_available():
    """True when an OCR engine is configured and importable."""
    if current_app.config['OCR_BACKEND'] == 'noop':
        return False
    try:
        import pytesseract  # noqa: F401

        return True
    except ImportError:
        return False


#: Cheap language detection: count stopwords rather than pull in a dependency
#: whose accuracy on a 200-word booking confirmation is no better.
_LANG_MARKERS = {
    'es': (' de ', ' la ', ' el ', ' que ', ' para ', ' con ', ' salida ',
           ' llegada ', ' reserva ', ' hotel ', ' vuelo ', ' habitación '),
    'en': (' the ', ' and ', ' your ', ' from ', ' departure ', ' arrival ',
           ' booking ', ' flight ', ' check-in ', ' reservation '),
    'fr': (' le ', ' les ', ' votre ', ' vol ', ' réservation ', ' départ ',
           ' arrivée ', ' hôtel '),
    'de': (' der ', ' die ', ' und ', ' ihre ', ' flug ', ' buchung ',
           ' abflug ', ' ankunft '),
}


def _detect_language(pages):
    """Guess the document's language from stopword frequency."""
    text = ' '.join(p.contenido for p in pages)[:4000].lower()
    if not text.strip():
        return None
    padded = f' {text} '
    scores = {
        lang: sum(padded.count(marker) for marker in markers)
        for lang, markers in _LANG_MARKERS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 3 else None
