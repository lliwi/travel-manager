"""Field-level provenance tracking.

Specification section 2.3: every stored value keeps its origin
(``documento`` / ``manual`` / ``ia``), a confidence indicator and, where
possible, the document and page it came from.

The origin boundary, stated once and applied everywhere:

* ``documento`` -- the value has a literal span in the document text, so
  ``fragmento`` and ``pagina`` are populated.
* ``ia`` -- the model inferred it with no literal span (a timezone from an
  airport code, a normalised carrier name).
* ``manual`` -- a person typed or corrected it.
"""
import logging

from app.extensions import db
from app.models.enums import ProvenanceOrigin
from app.models.itinerary import FieldProvenance

logger = logging.getLogger(__name__)

#: Below this confidence a field is always flagged for human review, whatever
#: the document said (specification section 2.3, final paragraph).
DEFAULT_REVIEW_THRESHOLD = 0.85


def record(entity, entity_kind, campo, origen, valor_actual, valor_anterior=None,
           confianza=None, document_id=None, extraction_id=None, pagina=None,
           fragmento=None, actor=None, commit=False):
    """Append one provenance row.

    Rows are never updated: the newest row for a ``(entidad, campo)`` pair is the
    current provenance and the older ones are the change history the
    specification asks for.
    """
    row = FieldProvenance(
        entidad_tipo=entity_kind,
        entidad_id=entity.id,
        campo=campo,
        origen=origen,
        confianza=confianza,
        document_id=document_id,
        extraction_id=extraction_id,
        pagina=pagina,
        fragmento=(fragmento[:2000] if fragmento else None),
        valor_anterior=_render(valor_anterior),
        valor_actual=_render(valor_actual),
        actor_id=getattr(actor, 'id', None),
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def record_manual_fields(entity, entity_kind, campos, actor, commit=False):
    """Mark a set of fields as manually entered."""
    rows = []
    for campo in campos:
        if not hasattr(entity, campo):
            continue
        rows.append(record(
            entity, entity_kind, campo,
            origen=ProvenanceOrigin.MANUAL,
            valor_actual=getattr(entity, campo),
            confianza=1.0,
            actor=actor,
        ))
    if commit:
        db.session.commit()
    return rows


def record_manual_change(entity, entity_kind, campo, anterior, nuevo, actor, commit=False):
    """Record a human correction of one field."""
    return record(
        entity, entity_kind, campo,
        origen=ProvenanceOrigin.MANUAL,
        valor_actual=nuevo,
        valor_anterior=anterior,
        confianza=1.0,
        actor=actor,
        commit=commit,
    )


def record_extracted_field(entity, entity_kind, campo, valor, extraction,
                           confianza=None, pagina=None, fragmento=None,
                           actor=None, commit=False):
    """Record a value that came out of a document.

    The origin is decided by whether a literal span was found: that is the whole
    distinction between ``documento`` and ``ia``.
    """
    origen = ProvenanceOrigin.DOCUMENTO if fragmento else ProvenanceOrigin.IA
    return record(
        entity, entity_kind, campo,
        origen=origen,
        valor_actual=valor,
        confianza=confianza,
        document_id=extraction.document_id if extraction else None,
        extraction_id=extraction.id if extraction else None,
        pagina=pagina,
        fragmento=fragmento,
        actor=actor,
        commit=commit,
    )


def current_for(entity, entity_kind):
    """The current provenance of every field of an entity.

    ``entity_kind`` is required, and that is the point. It used to default to
    the class name, which is «travelsegment» while every stored row says
    «segmento»: a caller who left it out got an empty result and no error, so
    the screen showed «nothing was extracted» for a row full of extracted
    fields. A missing argument is a better failure than a wrong answer.

    Returns:
        ``{campo: FieldProvenance}`` holding the newest row per field.
    """
    kind = entity_kind
    rows = (
        FieldProvenance.query
        .filter_by(entidad_tipo=kind, entidad_id=entity.id)
        .order_by(FieldProvenance.created_at.asc(), FieldProvenance.id.asc())
        .all()
    )
    current = {}
    for row in rows:
        current[row.campo] = row
    return current


def history_for(entity, entity_kind=None, campo=None):
    """Full provenance history of an entity, oldest first."""
    kind = entity_kind or type(entity).__name__.lower()
    query = FieldProvenance.query.filter_by(entidad_tipo=kind, entidad_id=entity.id)
    if campo:
        query = query.filter_by(campo=campo)
    return query.order_by(FieldProvenance.created_at.asc()).all()


def recalculate_rollup(entity, entity_kind, threshold=None, commit=False):
    """Refresh the denormalised confidence rollup on an itinerary entity.

    Keeping ``confianza_min`` and ``requiere_revision`` on the row itself lets
    the interface filter low-trust items without joining the provenance table on
    every listing.
    """
    threshold = threshold if threshold is not None else _review_threshold()
    current = current_for(entity, entity_kind)

    confidences = [
        float(row.confianza) for row in current.values()
        if row.confianza is not None
    ]

    if confidences:
        entity.confianza_min = min(confidences)
        entity.requiere_revision = min(confidences) < threshold
    else:
        entity.confianza_min = None
        # No provenance at all means nothing was ever recorded -- not a problem
        # to flag, since a purely manual entity has confidence 1.0 rows.
        entity.requiere_revision = False

    if commit:
        db.session.commit()
    return entity.confianza_min, entity.requiere_revision


def umbral_revision():
    """The confidence below which a field is worth a person's attention.

    Public because the screens need it too: marking a field as doubtful and
    deciding whether an item «requires review» have to use the same number, or
    the list and the form disagree about the same row.
    """
    return _review_threshold()


def _review_threshold():
    """The configured review threshold, falling back to the default."""
    try:
        from app.services import settings_service

        return settings_service.get_float(
            'DOCUMENTOS_UMBRAL_REVISION', DEFAULT_REVIEW_THRESHOLD
        )
    except Exception:
        return DEFAULT_REVIEW_THRESHOLD


def _render(value):
    """Stringify a value for the provenance record."""
    if value is None:
        return None
    from datetime import date, datetime

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)[:2000]
