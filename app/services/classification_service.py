"""Rule-based document classification (specification section 2.3 step 3).

Rules run before any model does. A boarding pass with an IATA pair and a PNR is
unambiguous, and recognising it with a regex is faster, free and more
reproducible than an inference call. The model is consulted only when the rules
are genuinely unsure.
"""
import logging
import re

from app.models.enums import DocumentClassification

logger = logging.getLogger(__name__)

#: ``clasificacion: ((patron, peso), ...)``. Weights are additive and the score
#: is normalised against the best possible score for that class.
PATTERNS = {
    DocumentClassification.VUELO: (
        (re.compile(r'\b[A-Z]{3}\s*(?:-|–|→|>|to|a)\s*[A-Z]{3}\b'), 3.0),
        (re.compile(r'(?i)\b(tarjeta de embarque|boarding\s*pass)\b'), 3.0),
        (re.compile(r'(?i)\b(vuelo|flight)\s*[:nº#]?\s*[A-Z]{2}\s?\d{2,4}\b'), 3.0),
        (re.compile(r'(?i)\b(iberia|vueling|ryanair|air europa|lufthansa|air france|'
                    r'klm|british airways|easyjet|tap|swiss|emirates|qatar)\b'), 2.0),
        (re.compile(r'(?i)\b(puerta de embarque|gate|terminal)\b'), 1.0),
        (re.compile(r'(?i)\b(equipaje de mano|facturado|checked baggage)\b'), 1.0),
        (re.compile(r'(?i)\b(localizador|pnr|booking reference)\b'), 1.0),
    ),
    DocumentClassification.TREN: (
        (re.compile(r'(?i)\b(renfe|ave|ouigo|iryo|avlo|sncf|trenitalia|italo|'
                    r'deutsche bahn|db bahn|eurostar|thalys|alvia|euromed)\b'), 3.0),
        (re.compile(r'(?i)\b(billete de tren|train ticket|tren\s*[:nº#])\b'), 3.0),
        (re.compile(r'(?i)\b(coche|voiture|carriage)\s*[:nº#]?\s*\d+\b.*'
                    r'\b(plaza|asiento|seat)\b'), 2.0),
        (re.compile(r'(?i)\b(estaci[oó]n|gare|bahnhof|station)\b'), 1.5),
        (re.compile(r'(?i)\b(and[eé]n|v[ií]a|platform)\b'), 1.0),
    ),
    DocumentClassification.HOTEL: (
        (re.compile(r'(?i)\b(check[\s-]?in)\b.*\b(check[\s-]?out)\b', re.S), 3.0),
        (re.compile(r'(?i)\b(confirmaci[oó]n de reserva|booking confirmation)\b.*'
                    r'\b(hotel|apartamento|alojamiento)\b', re.S), 3.0),
        (re.compile(r'(?i)\b(hotel|hostal|apartamento|aparthotel|resort|b&b)\b'), 2.0),
        (re.compile(r'(?i)\b(habitaci[oó]n|room)\s+(doble|individual|twin|double|single)'
                    r'\b'), 2.0),
        (re.compile(r'(?i)\b(\d+\s+noches?|\d+\s+nights?)\b'), 2.0),
        (re.compile(r'(?i)\b(desayuno incluido|media pensi[oó]n|pensi[oó]n completa|'
                    r'solo alojamiento|breakfast included)\b'), 1.5),
        (re.compile(r'(?i)\b(booking\.com|expedia|hoteles\.com|nh hoteles|meli[aá]|'
                    r'barcel[oó]|riu|marriott|hilton|accor)\b'), 1.5),
    ),
    DocumentClassification.VEHICULO: (
        (re.compile(r'(?i)\b(alquiler de (?:coche|veh[ií]culo)|car rental|'
                    r'rent a car)\b'), 3.0),
        (re.compile(r'(?i)\b(recogida|pick[\s-]?up)\b.*\b(devoluci[oó]n|drop[\s-]?off|'
                    r'return)\b', re.S), 3.0),
        (re.compile(r'(?i)\b(hertz|avis|europcar|sixt|enterprise|budget|goldcar|'
                    r'record go|centauro)\b'), 2.5),
        (re.compile(r'(?i)\b(franquicia|dep[oó]sito|excess|deductible)\b'), 1.5),
        (re.compile(r'(?i)\b(categor[ií]a|grupo|group)\s*[:]?\s*[A-Z]{1,3}\b.*'
                    r'\b(o similar|or similar)\b', re.S), 1.5),
        (re.compile(r'(?i)\b(matr[ií]cula|licence plate|kil[oó]metraje|mileage)\b'), 1.0),
    ),
    DocumentClassification.SEGURO: (
        (re.compile(r'(?i)\b(p[oó]liza|policy)\s*[:nº#]?\s*[A-Z0-9]{5,}\b'), 3.0),
        (re.compile(r'(?i)\b(seguro de viaje|travel insurance|asistencia en viaje)\b'), 3.0),
        (re.compile(r'(?i)\b(cobertura|coverage|capital asegurado|asegurado)\b'), 2.0),
        (re.compile(r'(?i)\b(mapfre|axa|allianz|generali|iati|intermundial|'
                    r'chapka|world nomads)\b'), 2.0),
        (re.compile(r'(?i)\b(tel[eé]fono de (?:asistencia|emergencia)|'
                    r'emergency assistance)\b'), 1.5),
    ),
    DocumentClassification.VISADO: (
        (re.compile(r'(?i)\b(visado|visa)\b.*\b(concedido|approved|granted|'
                    r'autorizaci[oó]n)\b', re.S), 3.0),
        (re.compile(r'(?i)\b(esta|eta|evisa|e-visa|schengen visa)\b'), 3.0),
        (re.compile(r'(?i)\b(permiso de entrada|entry permit|autorizaci[oó]n de '
                    r'viaje)\b'), 2.5),
        (re.compile(r'(?i)\b(n[uú]mero de (?:entradas|visado)|number of entries)\b'), 2.0),
        (re.compile(r'(?i)\b(estancia m[aá]xima|maximum stay|duration of stay)\b'), 1.5),
        (re.compile(r'(?i)\b(pasaporte|passport)\s*[:nº#]?\s*[A-Z0-9]{6,}\b'), 1.5),
    ),
}

#: Filename hints. Weaker than content, because a file name is trivially wrong.
FILENAME_HINTS = {
    DocumentClassification.VUELO: ('vuelo', 'flight', 'boarding', 'embarque', 'billete'),
    DocumentClassification.TREN: ('tren', 'train', 'renfe', 'ave', 'ouigo'),
    DocumentClassification.HOTEL: ('hotel', 'alojamiento', 'booking', 'reserva'),
    DocumentClassification.VEHICULO: ('coche', 'car', 'rental', 'alquiler', 'vehiculo'),
    DocumentClassification.SEGURO: ('seguro', 'insurance', 'poliza', 'policy'),
    DocumentClassification.VISADO: ('visado', 'visa', 'esta', 'permiso'),
}

FILENAME_WEIGHT = 1.0

#: Below this, the rules admit they do not know and the model is consulted.
MIN_CONFIDENCE = 0.7


def classify_by_rules(texto, nombre_archivo=None):
    """Classify a document from its text and name.

    Returns:
        ``(clasificacion, confianza)``. The confidence is the winning score
        relative to the runner-up, so "matched a lot of hotel words and nothing
        else" scores high while "matched a bit of everything" scores low --
        which is exactly when a model's opinion is worth paying for.
    """
    if not texto and not nombre_archivo:
        return DocumentClassification.DESCONOCIDO, 0.0

    haystack = (texto or '')[:12000]
    filename = (nombre_archivo or '').lower()

    scores = {}
    for classification, patterns in PATTERNS.items():
        score = sum(weight for pattern, weight in patterns if pattern.search(haystack))
        for hint in FILENAME_HINTS.get(classification, ()):
            if hint in filename:
                score += FILENAME_WEIGHT
                break
        scores[classification] = score

    if not any(scores.values()):
        return DocumentClassification.DESCONOCIDO, 0.0

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

    # A clear win over the runner-up is what makes the answer trustworthy; a
    # narrow win means the document looks like two things at once.
    total = best_score + runner_up_score
    separation = (best_score - runner_up_score) / total if total else 0.0
    strength = min(best_score / 6.0, 1.0)
    confidence = round(min(0.35 + 0.65 * (0.5 * strength + 0.5 * separation), 0.99), 2)

    return best, confidence


def needs_ai_classification(confianza, threshold=MIN_CONFIDENCE):
    """True when the rules were not confident enough to decide alone."""
    return confianza < threshold
