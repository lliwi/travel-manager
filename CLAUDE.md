# CLAUDE.md

Guidance for working on Travel Manager. The authoritative specification is
`requerimiento_tecnico_gestor_viajes_ia.md`; this file records the decisions
taken while implementing it and the invariants that must not be broken.

## Project overview

Corporate travel management with AI-assisted document extraction. Managers
create trips, attach booking documents, assign travellers, and the system
extracts structured data, consolidates an itinerary, detects logistical problems
and produces destination security advisories.

Phase 1 of the specification is implemented, and most of phases 2 and 3:
advanced OCR and every document type, official-source connectors with change
monitoring, notifications and reports, corporate policy with costs, and
continuous model evaluation.

Two things remain, deliberately last:

- **Travel-provider integrations** (Amadeus, Renfe, a corporate agency) --
  meaning *booking*. Still not startable without a contract and credentials.
  What exists now is one step short of it: `travel_search_service` reads real
  flights and lodging through SerpApi's Google Travel engines, so the planning
  assistant shows what exists instead of only how to get there. Three limits
  that are properties of the connector, not gaps to close later: it books
  nothing, its prices are Google's and therefore indicative, and it has no
  train engine at all -- for rail the assistant orients as before and says so,
  because an empty list must never read as «there is no way to get there».
- **Group sync and MFA/SSO.** AD/LDAP sign-in itself is done:
  `identity/ldap.py` authenticates against a directory, and the two kinds of
  account coexist -- turning the directory on disables no local account, which
  is what keeps an administrator from being locked out when the directory is
  unreachable. What remains is populating `role_group_mappings` so directory
  groups drive roles automatically; today a provisioned account gets `usuario`
  and anything above that is granted by hand, which is the safe default and
  never taken back by a later login.

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

7. **AI configuration lives in the database, not the environment.** Provider,
   endpoint, model, API keys and the per-task bindings are administered from
   the panel. Changing a model is an administrative decision, not a redeploy,
   and a key in the environment is a key in plain text. Only two bootstrap
   values remain in config, used to seed the first provider on a fresh install.

8. **What leaves is recorded.** `ai/guard.py` no longer refuses anything: the
   decision is which provider a task is bound to, made in the panel. Every call
   still writes an `ai_runs` row naming the provider, the model, the purpose and
   the trip, so "what did we send outside, and when" stays answerable. That
   record is now the whole of the control. See the departures below.

9. **Untrusted content is delimited and schema-bound.** Document text and web
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

- **Nothing is withheld from an external provider** (§2.5 asks for documents
  and PII to be gated by default, each behind its own switch). Binding a task to
  a provider is already the decision: it is made in Administración → Proveedores
  de IA, by an administrator, knowingly, and recorded. A second switch
  confirming they meant it ends up permanently on, which is a control in name
  only. What replaces it is the `ai_runs` trail, and `ai/guard.py` keeps its
  hook so a rule has one place to go if an organisation needs one.

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

## What the AI is allowed to restate

The planning assistant receives real flight and hotel rows and may reason about
them -- which option is better, which connection is tight -- but must not repeat
their times or prices. The screen renders those from the connector's own data.
A model that restates a price and drops a digit leaves two figures on the page
and no way to tell which one is real, so the numbers have exactly one source.
The rows themselves arrive as an `UntrustedBlock` like any other outside
content: a hotel named «ignora tus instrucciones» is content, not an order.

## Observability

`/healthz` and `/readyz` say whether the system is alive; `/metrics` says how
it is doing. Business figures are **queried from the database at scrape time**,
never accumulated in memory: four Gunicorn workers mean four sets of counters,
and the one that answers reports a quarter of the truth. That choice is also
what makes the Celery worker observable without a port -- its outcomes land in
those tables. Only request latency is counted in-process, which is why
`docker/flask/gunicorn.conf.py` exists.

`/metrics` is not public and cannot be made public by forgetting something: the
`METRICS_TOKEN` bearer or a private source address, with Nginx denying it at
the edge as well. It is exempt from the session barrier in
`tests/test_authorization.py` because a scraper cannot log in; its real
controls are tested in `tests/test_metricas.py`.

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
$COMPOSE exec web flask apply-retention      # simula; --execute borra
$COMPOSE logs -f worker

$COMPOSE exec web flask evaluar              # conjunto dorado; --modelo para probar otro
$COMPOSE exec web flask ai-stats             # cómo se ha portado cada modelo

./scripts/backup.sh                         # base de datos + documentos, juntos
./scripts/restore.sh --from backups/backup_AAAAMMDD_HHMMSS
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
