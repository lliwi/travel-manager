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
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError:
        return {}

    if hasattr(stream, 'seek'):
        stream.seek(0)
    data = stream.read()
    dpi = current_app.config['OCR_DPI']
    languages = current_app.config['OCR_LANGUAGES']

    results = {}
    for number in page_numbers:
        try:
            images = convert_from_bytes(
                data, dpi=dpi, first_page=number, last_page=number
            )
            if not images:
                continue
            text = pytesseract.image_to_string(
                _preprocess(images[0]), lang=languages, config='--psm 6'
            )
            results[number] = PageText(number, text.strip(), uso_ocr=True)
        except Exception as exc:
            logger.warning('Falló el OCR de la página %s: %s', number, exc)

    return results


def _extract_image(stream):
    """OCR a scanned image."""
    if not _ocr_available():
        raise ProcessingError(
            'El documento es una imagen y el motor de OCR no está disponible.'
        )

    import pytesseract
    from PIL import Image

    if hasattr(stream, 'seek'):
        stream.seek(0)
    image = Image.open(stream)
    text = pytesseract.image_to_string(
        _preprocess(image),
        lang=current_app.config['OCR_LANGUAGES'],
        config='--psm 6',
    )
    page = PageText(1, text.strip(), uso_ocr=True)
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


def _preprocess(image):
    """Greyscale and threshold an image, which measurably helps Tesseract."""
    try:
        grey = image.convert('L')
        return grey.point(lambda p: 255 if p > 160 else 0)
    except Exception:
        return image


_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\n{3,}')


def _strip_html(html):
    """Crude HTML-to-text, adequate for a booking confirmation email."""
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', text)
    text = _TAG_RE.sub(' ', text)
    import html as html_module

    text = html_module.unescape(text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return _WS_RE.sub('\n\n', text).strip()


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
