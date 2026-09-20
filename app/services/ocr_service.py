"""Text extraction and OCR.

Specification section 2.3 step 2 and section 6: a local engine is preferred,
behind an interface that a permitted external service could replace.

Text is taken from a PDF's text layer when there is one, and OCR is used only
when there is not -- OCR is slow and lossy, so running it on a digitally
generated booking PDF would make extraction worse, not better.
"""
import logging
import re

from flask import current_app

from app.utils.errors import PermanentError, ProcessingError

logger = logging.getLogger(__name__)


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


def _extract_email(stream):
    """Read the text parts of an EML message."""
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
        parts.append(content.strip())

    attachments = [
        part.get_filename() for part in message.iter_attachments()
        if part.get_filename()
    ]
    if attachments:
        parts.append('Adjuntos: ' + ', '.join(attachments))

    page = PageText(1, '\n\n'.join(p for p in parts if p))
    return ExtractedText([page], motor='email', uso_ocr=False,
                         idioma=_detect_language([page]))


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
