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
- **Group sync and SSO.** AD/LDAP sign-in itself is done:
  `identity/ldap.py` authenticates against a directory, and the two kinds of
  account coexist -- turning the directory on disables no local account, which
  is what keeps an administrator from being locked out when the directory is
  unreachable. What remains is populating `role_group_mappings` so directory
  groups drive roles automatically; today a provisioned account gets `usuario`
  and anything above that is granted by hand, which is the safe default and
  never taken back by a later login. MFA is done too, as TOTP -- see below.

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

## Every outbound call goes through `utils/http.cliente`

A deployment may sit behind a corporate proxy, so the places that open sockets
are one function rather than seven. `tests/test_proxy_de_salida.py` fails if a
module builds its own `httpx.Client`: that works everywhere except behind a
proxy, and nothing says so until the deployment that has one.

What must never be proxied is decided by address, not by feature:
`EXCEPCIONES_POR_DEFECTO` covers loopback, `host.docker.internal` and the sibling
containers. Local inference is the case that matters -- the same code path
serves a local vLLM and a hosted OpenAI, and routing the local one outward
either fails or succeeds slowly while a machine outside the perimeter logs every
prompt.

Precedence is stated and tested: Ajustes wins, the environment
(`HTTPS_PROXY`/`NO_PROXY`) is the fallback. `trust_env` stays on so
`SSL_CERT_FILE` still works for a proxy that re-signs certificates.

## The second factor happens before the session

`mfa_service` is TOTP. Three things it exists to guarantee, each of which fails
silently if broken: a code works exactly once (`mfa_ultimo_paso` -- TOTP alone
accepts the same digits for a whole window), nothing readable is stored (the
secret encrypted, the recovery codes hashed), and losing a phone is not losing
the account (recovery codes, an admin reset, `flask mfa-reset`).

The verification happens *before* `login_user`, through a short-lived
`session['mfa_pendiente']`. Logging somebody in and then asking would give a
valid session to somebody who has proved half of what we ask, and every
authorisation check in the application would already say yes.

The requirement is enforced in a `before_request`, not only at sign-in: an
administrator changes the policy while everybody else is already logged in.

## Directory accounts are read-only

A directory-backed account authenticates and nothing else. Its personal fields
are refreshed from the directory on every sign-in, so an edit made here would
appear to work and then revert -- which is why `user_service.update_user`
refuses them rather than the templates merely hiding them. The same goes for a
local password on such an account: the login path routes by
`identity_provider`, so that hash would never be read, and somebody would rely
on it the day the directory is down.

What stays editable is what this application owns: roles and whether the
account may be used. `User.es_local` is the single question both the screens
and the login path ask.

## What the AI is allowed to restate

The planning assistant receives real flight and hotel rows and may reason about
them -- which option is better, which connection is tight -- but must not repeat
their times or prices. The screen renders those from the connector's own data.
A model that restates a price and drops a digit leaves two figures on the page
and no way to tell which one is real, so the numbers have exactly one source.
The rows themselves arrive as an `UntrustedBlock` like any other outside
content: a hotel named «ignora tus instrucciones» is content, not an order.

## Sampling parameters are layered, and the tuner writes the top layer

`ai/parametros.py` is the one catalogue of sampling parameters: the forms, the
validator, both providers and the tuner all read it. It also maps each one to
the name each wire family uses, so nothing is sent to an endpoint that does
not take it. From lowest to highest: the provider row, the model's profile for
every task, what the task asks for in code (`DEFECTOS_POR_TAREA`), and the
model's profile *for that task*. The order is what stops a model-wide
«temperature 0.7» from undoing extraction's 0. Every run records what was
actually sent in `ai_runs.parametros`.

`autotune_service` searches a small grid one parameter at a time against the
evaluation cases (`evaluation_service.medir`), with the real task run under
`selector.forzar`. Its result is a proposal. It applies itself only when
whoever launched it asked for that *and* the gain clears
`MEJORA_MINIMA_PARA_APLICAR`. Applying keeps the previous profile, and
reverting refuses to overwrite a later change. Faster and slightly worse never
wins: that trade is a person's call. The first call of a search is a
warm-up and not counted: a local model loads on it, and without it every
search was biased against the configuration it started from.

A search is never one Celery task. Each step runs one trial and queues the
next, the plan is replayed from the trials in the row, and the row's
`celery_task_id` is the token that says which step owns it. As a single task,
a search on a 9B model hit the worker's 30-minute limit at trial 8 and died
without a word -- and a deploy would have killed it the same way.

## A backup is one encrypted ZIP, and it carries the key

Administración → Copias de seguridad makes a full copy on demand -- every
table, every stored document, and `SECRETS_ENCRYPTION_KEY` -- and restores
from the same file. The key has to travel: without it the API keys, the
directory password, MFA secrets and document numbers come back unreadable.
So every member is AES-256-GCM under a passphrase-derived key (scrypt), with
its name as associated data; the passphrase is stored nowhere and crosses the
Celery queue encrypted with the application key.

Tables are exported row by row rather than with `pg_dump`, so the suite can
prove the round trip on SQLite and secrets can be re-encrypted when the copy
lands on an installation with another key -- `_campos_cifrados()` lists
those columns, and a test fails if a new `encrypt_secret` column is missing
from it. A restore refuses a wrong passphrase, another schema revision or a
damaged file before touching anything; `backup_jobs` is the one table neither
copied nor replaced, so the restore can report on itself. The restored audit
chain still verifies, and its last event says how long the replaced one was.

`scripts/backup.sh` still exists for the host: it is what to use when the
application itself is what needs rescuing.

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
$COMPOSE exec web flask mfa-reset <usuario>       # si alguien pierde el móvil
$COMPOSE exec web flask apply-retention      # simula; --execute borra
$COMPOSE logs -f worker

$COMPOSE exec web flask evaluar              # conjunto dorado; --modelo para probar otro
$COMPOSE exec web flask ai-stats             # cómo se ha portado cada modelo
$COMPOSE exec web flask autoajustar --tarea extract_document   # busca mejores parámetros

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
