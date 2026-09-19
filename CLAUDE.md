# CLAUDE.md

Guidance for working on Travel Manager. The authoritative specification is
`requerimiento_tecnico_gestor_viajes_ia.md`; this file records the decisions
taken while implementing it and the invariants that must not be broken.

## Project overview

Corporate travel management with AI-assisted document extraction. Managers
create trips, attach booking documents, assign travellers, and the system
extracts structured data, consolidates an itinerary, detects logistical problems
and produces destination security advisories.

Phase 1 of the specification is implemented. Phases 2 and 3 (AD/LDAP,
official-source connectors, notifications, travel-provider integrations) are
designed for but not built.

## Architecture

**Stack**: Flask 3 application factory · PostgreSQL 16 · SQLAlchemy 2 + Alembic ·
Celery + Redis · MinIO · ClamAV · Tesseract · Ollama (host) · Jinja2 +
Bootstrap 5 · Gunicorn behind Nginx · Docker Compose.

**Two surfaces, one brain.** The Jinja blueprints and `/api/v1` are different
renderings of the same services. They never duplicate a decision; where they
differ is only in how an exception becomes a response.

## Invariants

These are the things that will break the system quietly if violated. Each has a
test that fails by name.

1. **Authorisation exists once.** Every access question goes through
   `authorization_service.can()`. Route decorators are thin wrappers. A new
   `/api/v1` endpoint without `__authz__` fails `tests/test_authorization.py`.

2. **A traveller never sees another traveller's rows.** Enforced by
   `scope_itinerary_query` / `scope_itinerary_items`, which the timeline, the
   alert list, the document list and `build_ai_context` all use. The AI cannot
   leak what it was never given — that is a structural property, not a prompt
   instruction.

3. **Instants are triples.** Every itinerary time is `_local` + `_tz` + `_utc`.
   Only `timeutil.set_instant` writes them. Writing a `_utc` column anywhere
   else lets the three drift apart and silently corrupts every margin the alert
   engine computes.

4. **Only `document_service.transition` writes `estado_proceso`.** The
   transition table is what makes a failed pipeline diagnosable and a partial
   reprocess possible. `tests/test_documents.py` greps for violations.

5. **The audit trail is append-only and chained.** No updates, no deletes, and
   the digest must not depend on how the driver returns a timestamp — otherwise
   every row looks tampered with after a restore.

6. **Alerts reconcile, never accumulate.** Re-running the engine must not
   duplicate an alert, must refresh changed evidence, must close what no longer
   reproduces, and must never resurrect one a manager accepted or dismissed.

7. **Documents and PII do not leave by default.** `ai/guard.py` refuses to route
   a sensitive payload to an external provider unless an administrator enabled
   it, and records the refusal as a blocked `ai_runs` row.

8. **Untrusted content is delimited and schema-bound.** Document text and web
   content enter prompts only as `UntrustedBlock`, and every answer is validated
   against a JSON schema. Injected instructions have nowhere to land.

## Deliberate departures from the specification

Each is a considered decision, not an oversight:

- **`audit_events` uses BIGSERIAL, not UUID** (§4 says UUID everywhere). It is
  an append-only log no business foreign key references; a monotonic key gives
  ordered inserts, a compact index and cheap keyset pagination.

- **A user with no relationship to a trip gets 404, not 403** (§5.1 says 403).
  Access is refused either way; 404 additionally does not confirm the trip
  exists. A user who *is* on the trip but lacks a specific permission still gets
  403.

- **Enum values are ASCII snake_case**, with the accented Spanish as a label.
  The specification writes states like `en preparación`; that is the label.

- **`organizations` exists but is unused.** §2.1 promises a future
  organisational scope; two nullable columns now beat a backfill across every
  trip and user later. This is the only speculative schema in the model.

## Conventions

- **Code, docstrings and comments in English. Interface, flash messages, enum
  labels, prompts and documents in Spanish.** Domain column names are Spanish
  (`nombre`, `fecha_caducidad`); infrastructure columns are English
  (`created_at`, `is_deleted`).
- Business logic lives in `app/services/`, never in a route.
- Models carry their own display helpers and `to_dict()`; routes do not format.
- Comments explain *why*, never *what*. If a line needs a comment to say what it
  does, rewrite the line.
- Records are soft-deleted. Retention-driven erasure is a separate, explicit,
  audited operation.

## Common commands

```bash
./setup.sh                                  # first install
./start.sh                                  # daily start
pytest                                      # whole suite
pytest -m acceptance                        # the specification's section 5
pytest -m security                          # authorisation and AI controls

COMPOSE="docker compose -f docker/docker-compose.yml"
$COMPOSE exec web flask db upgrade
$COMPOSE exec web flask seed
$COMPOSE exec web flask ai-health
$COMPOSE exec web flask verify-audit
$COMPOSE logs -f worker
```

## Testing

The suite runs against SQLite with every external dependency substituted:
storage in memory, no antivirus, no OCR, a deterministic AI provider. A test
must never pass or fail because a real service happened to be reachable.

`tests/test_acceptance.py` maps one-to-one onto specification section 5. If one
of those fails, the system does not meet the requirement, whatever else passes.

## Development principles

1. **Refuse rather than guess.** An unresolved timezone, an unknown country or a
   low-confidence field lowers confidence and asks a human. Defaulting silently
   to the permissive answer is how a safety check stops protecting anyone.
2. **Make the safe thing structural.** Prefer a query that cannot return the
   wrong rows over a check that might be forgotten.
3. **Audit everything sensitive**, and make the trail verifiable.
4. **A human confirms before data becomes real.** Extraction proposes; a manager
   decides. Record who decided.
5. **Keep the AI at arm's length.** It reads what the asker may already see, it
   answers against a schema, and its references are validated back against the
   authorised set.
6. **Local first.** Inference, OCR and antivirus all run inside the perimeter by
   default; anything leaving it is an explicit administrative decision.
