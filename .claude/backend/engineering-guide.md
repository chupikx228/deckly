# Backend engineering guide

How the generation backend should be written. This document is **stack-agnostic** on purpose:
it prescribes structure, principles and practices, not a language or framework. Choose the
stack that fits the team; every rule below holds regardless of that choice.

Two companion files in this folder are normative and take precedence over anything here when
they disagree:

- [`openapi.yaml`](openapi.yaml) — the machine-readable source of truth for the wire shape.
- [`api-contract.md`](api-contract.md) — the human-readable contract: endpoints, error codes,
  note field shapes, non-functional requirements.

This guide covers everything _around_ that contract: architecture, SOLID, boundaries,
testing, observability, security. Read the contract first, then this.

## The one thing to remember

The app is **offline-first**. Local SQLite owns all user data; scheduling runs on the device.
The backend exists for exactly one job: **turn a topic into a set of reviewable notes.** It is
a stateless generation service with a job queue in front of it — not the system of record.

Everything else follows from that. No deck/note/card CRUD, no reviews, no auth, no sync in v1.
If a feature seems to need one of those, it is a contract change (raise it), not a quiet
addition.

## Guiding principles

1. **The contract is the boundary.** The OpenAPI spec is the source of truth. Validate every
   request against it on the way in and every response against it on the way out. The client
   already parses responses with schemas generated from this spec — if the server drifts, the
   client rejects the payload. Treat a schema mismatch as a server bug.
2. **Determinism at the edges, non-determinism in the core.** The AI and web-search parts are
   inherently unpredictable. Push that unpredictability into a small, well-isolated core and
   keep the transport, validation, persistence and error layers boring and deterministic.
3. **Fail loud internally, fail gracefully externally.** Internally: crash on programmer
   error, log with context, alert. Externally: never leak a stack trace; always return a
   typed `Problem` the client can map to a translated message.
4. **Smallest surface that satisfies the contract.** Do not add endpoints, fields, headers or
   config the contract does not require. Every extra is a maintenance and security cost.

## Every backend change updates the contract first

The spec is the source of truth, and it is written **before** the code. So whenever you add
or change anything the client can observe, update this folder in the **same** change — never
after, never "later". This is a rule, not a suggestion; CI fails when code and spec disagree.

Applies to: a new endpoint, a new field on a request or response, a new `noteType`, a new job
stage, a new error `code`, a changed status code, a new required header, or a changed
validation rule.

For each such change, in order:

1. Edit [`openapi.yaml`](openapi.yaml) — add or change the operation, schema, parameter or
   enum. This is the authoritative edit; everything else follows from it.
2. Regenerate the client types: `pnpm api:generate`. Never hand-edit
   `frontend/packages/api-contract/src/generated`.
3. Update the human-readable [`api-contract.md`](api-contract.md) so its endpoint table, error
   table and field shapes still match the spec.
4. If the change alters _how_ the backend should be built (a new stage, a new provider port),
   add it to this guide too.
5. Run `pnpm api:lint` and confirm the generated types are in sync (both are CI gates).

If the change touches something the app is not allowed to gain (a deck/note/card CRUD, review,
auth or sync endpoint — see the locked decisions), it is not a routine addition: raise it as a
contract change and get sign-off first.

Adding an endpoint or field to the backend without writing it here is the one thing that
breaks the offline-first, mock-first workflow. The mobile app is developed against the spec;
an undocumented endpoint does not exist as far as the client is concerned.

## Architecture: ports and adapters

Structure the service as concentric layers. Dependencies point **inward only**. The inner
layers know nothing about HTTP, the database, the model provider or the search provider.

```
        ┌───────────────────────────────────────────┐
        │  Transport / API layer                     │   HTTP, routing, headers,
        │  (adapters: inbound)                        │   auth-less identity, serialization
        │   ┌───────────────────────────────────┐    │
        │   │  Application layer                 │    │   use cases / orchestration:
        │   │  (services, orchestrators)         │    │   "run a generation job",
        │   │   ┌───────────────────────────┐    │    │   "regenerate one note"
        │   │   │  Domain layer              │    │    │   entities, value objects,
        │   │   │  (pure business rules)     │    │    │   invariants, no I/O
        │   │   └───────────────────────────┘    │    │
        │   └───────────────────────────────────┘    │
        │  Infrastructure (adapters: outbound)        │   model provider, web search,
        │                                             │   image search, job store, cache
        └───────────────────────────────────────────┘
```

- **Domain** — the note types, the deck, the job state machine, the invariants (cloze markers
  numbered from 1 with no gaps; distractors never contain the answer; progress is monotonic).
  Pure. No HTTP, no SDK types, no clock reads, no randomness pulled in directly — those are
  passed in.
- **Application** — orchestrates a use case by calling _ports_ (interfaces). "Generate a deck"
  is: plan → retrieve sources → parse → generate cards → fetch media → finalize. It does not
  know _which_ model or _which_ search engine; it depends on the interface.
- **Infrastructure** — concrete adapters that implement the ports: a specific model client, a
  specific search provider, the job store, the cache. Swappable without touching the core.
- **Transport** — maps HTTP to use-case calls and back. Owns request validation, header
  handling (`Idempotency-Key`, `X-Client-Id`, `Accept-Language`), status codes, and the
  translation of a domain/application error into an RFC 9457 `Problem`.

The point of this shape: the expensive, flaky, changeable parts (model, search, image
providers) sit at the outer edge behind interfaces, and can be replaced, mocked or upgraded
without rewriting the logic that depends on them.

## SOLID, applied to this service

SOLID is not decoration here — each letter maps to a concrete decision in a generation
backend.

### S — Single Responsibility

Each unit has one reason to change.

- The **topic planner**, **source retriever**, **source parser**, **card generator**,
  **media fetcher** and **finalizer** are separate. They correspond one-to-one to the job
  stages in the contract, which is not a coincidence — the stages _are_ the responsibilities.
- The thing that talks HTTP is not the thing that runs the pipeline is not the thing that
  calls the model. When the model provider changes, the HTTP layer must not need editing.
- A "god service" that does validation, orchestration, model calls and persistence is the
  most common failure mode. If a class or module has more than one of those, split it.

### O — Open/Closed

Open for extension, closed for modification.

- Adding a new `noteType` should mean adding a generator/validator for it, **not** editing a
  switch that every existing type flows through. Register note-type handlers in a map keyed by
  type; the pipeline looks the handler up.
- Adding a new job stage should mean adding a stage implementation to an ordered list, not
  rewriting the orchestrator.

### L — Liskov Substitution

Any implementation of a port must be usable wherever the port is expected, with no surprises.

- The real model adapter and a fake/stub adapter used in tests must honour the same contract:
  same error types, same timeout semantics, same shape of output. A test double that "mostly"
  behaves like the real thing but throws a different error class defeats the tests.
- The client already relies on this at the system level: the mobile app runs against a mock
  transport and the real one interchangeably. The server should have the same discipline
  internally.

### I — Interface Segregation

Small, purpose-specific interfaces.

- The card generator needs a "generate text" capability; the media fetcher needs a "search
  images" capability. Do not force both behind one fat `AiProvider` interface with ten
  methods where each caller uses two. A consumer should depend only on the methods it calls.
- Ports are defined by what the **application** needs, not by what the **provider SDK**
  happens to offer.

### D — Dependency Inversion

High-level policy does not depend on low-level detail; both depend on abstractions.

- The generation use case depends on `SourceRetriever`, `SourceParser`, `CardGenerator`,
  `MediaFetcher`, `JobStore` **interfaces**, defined in the inner layers. The concrete provider clients
  implement them and are injected at the composition root (startup).
- This is what makes the whole thing testable and the provider replaceable. Never construct a
  model client or open a DB connection inside domain or application code — receive it.

## Boundaries and validation

The system boundary is where trust changes. Validate hard there, trust freely inside.

- **Inbound:** every request body, header and path parameter is validated against the spec
  before any work starts. Reject unknown fields (the request schemas are `additionalProperties:
false`). Bad input → `400 VALIDATION_FAILED`, never a 500.
- **Outbound to the client:** validate the response against the spec before sending. A note
  whose `fields` do not match its `noteType` must never reach the wire — the client will drop
  it, and worse, it signals the generator is producing garbage.
- **Outbound to providers:** treat model and search output as **untrusted**. It is generated,
  it can be malformed, prompt-injected, or policy-violating. Parse and validate it into your
  domain types at that boundary; do not pass raw provider output through to the client.
- Keep provider SDK types out of the domain. Map them to your own types at the adapter. When
  the SDK changes shape, only the adapter changes.

## The generation job: async and stateful-per-job

`POST /generations` enqueues and returns immediately; the client polls `GET
/generations/{jobId}`. This shape is mandated by the contract because generation takes
30–120s and does not fit one HTTP request.

- **The job is a state machine.** `queued → running → (succeeded | failed | cancelled)`. The
  last three are terminal. Model the transitions explicitly and reject illegal ones (cancel on
  a terminal job → `409 JOB_ALREADY_TERMINAL`).
- **`progress` is monotonic.** It must never decrease between polls. If you cannot compute a
  true fraction, derive it from the current stage's index — never let a retry walk it
  backwards.
- **A failed generation is not an HTTP error.** `GET` returns `200` with `status: "failed"`
  and a populated `error`. A 5xx means the _transport/polling_ failed, not the generation.
  Conflating them breaks the client's polling logic. This is the single most important error
  rule in the contract.
- **Cancellation is cooperative.** A cancelled job must actually stop consuming model/search
  budget, not just flip a flag while the pipeline runs to completion in the background.
- **Persist job state** in a store that survives a process restart and is shared across
  instances (see scaling). Jobs are retained ≥24h after completion so a user who backgrounded
  the app can return to a finished result.
- **No job stays non-terminal forever.** See "Scheduled housekeeping" below.

## Idempotency

`POST /generations` carries a client-generated `Idempotency-Key`.

- Replaying a request with the same key returns the **original** job, not a second one. Store
  `key → jobId` and look it up before enqueueing.
- Scope the key to the client (`X-Client-Id`) so two clients cannot collide.
- The client relies on this to survive network retries without spawning duplicate jobs. Treat
  it as a correctness requirement, not an optimization.
- Keys expire after `DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS` (24h), measured from the original
  request. Expiry clears the key on the job row (the column is nullable, and Postgres treats NULLs
  as distinct in the unique constraint); the row itself stays pollable until retention removes it.
  The key TTL must not exceed the job retention, or deleting a row would silently end its replay
  window early; config validation enforces this.

## Scheduled housekeeping

One Arq cron task, `run_housekeeping`, runs inside the worker every
`DECKLY_SWEEP_INTERVAL_MINUTES` (must divide 60; the task's timeout equals the interval so runs
never overlap, and Arq's `unique` cron ids mean only one worker runs each tick). Each run does,
in order, with every step capped at `DECKLY_SWEEP_BATCH_SIZE` rows and a failure in one step
logged without skipping the others:

1. **Stale `running` jobs** — no state change for `DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS`
   (default 15 min) are failed with `GENERATION_FAILED`. Config validation keeps this above the
   job timeout plus the job store timeout, so a live job, or one whose interruption is still
   being recorded, is never swept. Arq re-runs a crashed worker's job after its in-progress key
   expires, and `RunGeneration` fails it as abandoned; the sweep is the backstop for when that
   path cannot run (database down, the queue entry lost, retries exhausted).
2. **Stale `queued` jobs** — after `DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS` (default 1 h, never
   below the running threshold) are failed the same way.
3. **Expired idempotency keys** are cleared (see "Idempotency").
4. **Finished jobs** older than `DECKLY_CACHE_JOB_RETENTION_SECONDS` (at least 24h, measured from
   completion) are hard-deleted; polling one afterwards returns `404 JOB_NOT_FOUND`. Unfinished
   rows are never deleted. The backend is not the system of record, so there is no archive.

The sweep changes state only through `JobStore.update()`, and its transition re-checks the
locked row: the job must still have the status it was listed with and a last change older than
the cutoff, otherwise it is skipped. A worker that claims or advances a job while the sweep is
looking at it therefore wins cleanly, and a worker that arrives after the sweep gets
`JobAlreadyTerminalError` and stops without spending provider budget. A job the sweep cannot
update (for example a row whose stored stage no longer restores) is logged as
`stale_job_not_swept` and skipped, so one bad row cannot stall every later run.

A queued job whose saved request no longer validates (a contract change made it invalid after it
was stored) is not re-validated by the sweep. The worker fails it with `GENERATION_FAILED` as
soon as it picks it up; if it is never picked up, the queued threshold catches it.

## Error handling

- One error model: RFC 9457 `application/problem+json` with a stable machine-readable `code`.
  The full code table lives in [`api-contract.md`](api-contract.md); do not invent codes
  outside it.
- The client maps `code` → a translated message. It **never** renders `detail`. So `detail`
  is for logs and may be verbose and English; `code` is the contract.
- Convert exceptions to `Problem` in exactly one place (a transport-layer error mapper).
  Domain and application layers throw typed domain errors; the mapper decides the HTTP status
  and code. Do not build `Problem` objects scattered through the codebase.
- Never leak internal detail (stack traces, provider error text, SQL) to the client. Log it,
  return `INTERNAL_ERROR`.
- Distinguish **retryable** (`503 UPSTREAM_UNAVAILABLE`, timeouts) from **terminal**
  (`422 TOPIC_REJECTED`, `400 VALIDATION_FAILED`) failures, and set `retryAfterSeconds` on the
  ones worth retrying.

## Resilience with flaky dependencies

The model and search providers _will_ be slow, rate-limited or down. Design for it.

- **Timeouts on every outbound call.** No unbounded waits. A hung provider call must not hang
  a job forever.
- **Retries with backoff and jitter** for transient failures only — never retry a
  policy-rejection or a validation error.
- **Circuit breaker** around each provider so one failing dependency degrades gracefully
  (surface `UPSTREAM_UNAVAILABLE`) instead of piling up requests.
- **Bulkhead** the providers: image fetching being down must not stop card generation.
  `includeImages` is optional; a media failure should degrade to a deck without images, not
  fail the whole job.
- Make each pipeline stage independently retryable where possible, so a media hiccup does not
  re-run the expensive card generation.

## Caching

- Identical `(topic, language, cardCount, difficulty, noteTypes)` tuples should hit a
  server-side cache. "1000 most common English words" is requested by many users and should be
  generated once. Normalize the tuple (sort `noteTypes`, trim/lowercase where safe) before
  hashing it into the cache key.
- Cache the _validated domain result_, not raw provider output.
- Media URLs must stay valid ≥24h (the client caches media locally); a cache entry pointing at
  an expired URL is worse than a miss. The cache TTL (`DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS`,
  default 12h) is therefore validated at startup to stay below 24h, rather than re-checking media
  on every hit.
- The key covers every request field that shapes the output: `topic` (NFC, whitespace collapsed,
  case kept), `language` (lowercased), `cardCount`, `difficulty`, sorted `noteTypes`,
  `includeImages` and `instructions`. It is hashed, so request text never appears in a Redis key.
- The check runs in the worker right after the job is claimed, so a hit walks the normal job state
  machine. The cache fails **open**: a Redis outage, timeout or corrupt entry is a miss, and a
  failed write is logged and ignored. This is the opposite of the quota check, which fails closed
  because it guards spend. Only fully successful results are stored; a deck whose media step failed
  is returned but not cached, so an image outage is not pinned for the whole TTL.

## Rate limiting and abuse

- Rate-limit per `X-Client-Id`. Generation is expensive; one client must not exhaust the
  budget. Over the limit → `429 RATE_LIMITED` with `retryAfterSeconds`.
- `X-Client-Id` is an anonymous device id, not an identity — it is trivially spoofable. Use it
  for shaping, and add coarse IP-based limits as a backstop. Do not make security decisions on
  it alone.

## Content safety

- Reject topics that violate policy up front → `422 TOPIC_REJECTED`. The check runs
  synchronously in `POST /generations`, after the idempotency lookup and the quota reservation
  and before the job is stored, so a rejected topic never becomes a job. A rejection spends the
  quota unit (the check costs a model call); a check that fails or times out gives it back and
  answers `503 UPSTREAM_UNAVAILABLE`. A replay of an accepted job is never re-checked.
- Filter **generated** content for policy violations before it reaches the client. The model's
  output is untrusted; a topic passing the gate does not mean every generated card is safe.
  The filter runs right after generation, before media, and follows the same repair-or-drop rule
  as validation: a note without an explicit "allow" verdict is dropped, an unsafe deck title is
  replaced with one built from the topic, and only when no note is left does the job fail with
  `NO_VALID_CONTENT`. A classifier that cannot answer fails the job with `PROVIDER_UNAVAILABLE`.
- Screen **images** separately, because they are found after the text filter has run and their
  titles and descriptions are third-party wiki text. One batched classifier call per job judges
  every note's licensed candidates for the content policy, the image policy and relevance to the
  note, and ranks the acceptable ones; only a ranked candidate is attached. Images are best
  effort, so this check fails closed without failing the job: a refusal means no images, and an
  outage or an unreadable reply means no images and a result that is not cached. The image
  classifier has its own circuit breaker, so its outages never trip the text filter's.
- `POST /notes/regenerate` gets both text checks. The topic and the rejected note are screened
  together (`422 TOPIC_REJECTED`), alongside the search so the check costs no extra wall time, and
  the replacement note is screened before it is returned. With only one note there is nothing to
  drop and continue with, so a note that is not allowed is `503 NO_VALID_CONTENT`.
- Nothing unscreened is ever shipped, text or image.
- All three checks fail closed. A classifier refusal counts as a block, and a reply that cannot
  be read is an outage, never a pass.
- `sources` exist so the user can verify claims — they are a product feature, not decoration.
  Every returned note carries at least one real, reachable source; a note without one is
  dropped.
- Media without a known `license` must not be returned at all. Licensing is a legal
  requirement, not a nice-to-have. An attribution licence (CC BY, CC BY-SA) is accepted only
  when the author, title and source page can all be derived from the provider's metadata; the
  licence deed URL comes from the backend's own allowlist, never from the provider.

## Configuration and secrets

- All config from the environment; nothing hardcoded. Provider keys, model names, timeouts,
  rate limits, cache TTLs are config, not literals in code.
- Secrets (API keys) never in source, never in logs, never in error responses.
- Fail fast at startup if required config is missing — do not discover a missing key on the
  first user request.
- No magic numbers in code: timeouts, limits, retry counts, TTLs are named config values.

## Observability

You cannot debug a non-deterministic pipeline you cannot see.

- **Structured logs** (JSON), correlated by `jobId` and `X-Client-Id`, so one job's whole
  lifecycle is queryable. Log stage transitions, provider latencies, retries, and failures
  with enough context to reproduce — but never log secrets or full user content beyond what is
  needed.
- **Metrics:** job throughput, per-stage latency and failure rate, provider latency and error
  rate, cache hit rate, queue depth, rate-limit rejections. These are how you know the service
  is healthy before users tell you.
- **Traces** across the pipeline stages and outbound provider calls.
- `GET /health` reflects real dependency health (`ok` vs `degraded`) — the client shows an
  offline banner from it before the user fills in the wizard. A health check that always
  returns `ok` is worse than none.

How this is built is in `backend/README.md` ("Observability"). The rules for new code:

- **Event names** are `<subject>_<past-tense verb>` in snake_case; **fields** are snake_case
  (`job_id`, `client_id`, `request_id`, `duration_ms`), even where the API spells them `jobId`.
- **Correlation comes from context, not arguments.** Bind `job_id` / `request_id` with
  `correlated(...)` at the use-case entry; do not add them to `extra` in adapters. An explicit
  field always wins over the context, so a wrong explicit value silently mislabels a line.
- **Every outbound provider call goes through `ResilientCaller`** with its own
  `ProviderOperation`, which is what times, counts, traces and logs it.
- **Never log** keys, the topic or `instructions`, generated text, provider response bodies, URLs
  with query strings, or client addresses. Log counts and enum values. Exceptions logged with
  `exc_info` print their whole `__cause__` chain, so an SDK exception that carries a response body
  must not be chained (`from None`) once its useful part has been redacted into the new message.
- **Metric labels** take a fixed, small set of values (enums), never ids or user input.

## Testing

[`testing.md`](testing.md) is the full approach — how to test, the boundary-case checklist,
and what not to test. The summary:

- **Domain layer: pure unit tests, no I/O.** The invariants (cloze numbering, distractor
  rules, progress monotonicity, state-machine transitions) are cheap and critical to test
  here.
- **Application layer: tested against fake ports.** Inject stub `CardGenerator` /
  `SourceRetriever` etc. that return canned or deliberately malformed output, and assert the
  orchestrator handles success, partial failure, provider errors, cancellation and validation
  rejection correctly. No network in these tests.
- **Contract tests.** Every response the service can emit must validate against
  `openapi.yaml`. This is the cheapest possible insurance against the client rejecting
  payloads. Run it in CI.
- **Adapter/integration tests** for the real providers, run separately (they cost money and
  are flaky) — not in the fast unit suite.
- Test the failure paths as first-class citizens: provider timeout, provider garbage output,
  cancellation mid-stage, duplicate idempotency key, rate-limit trip. These are where a
  generation backend actually breaks.

## Scaling and statelessness

- **API instances are stateless.** No job state in process memory — it lives in the shared job
  store. Any instance can serve any client's poll. This is what lets you run more than one
  instance and restart without dropping jobs.
- Generation **workers** consume the queue and are scaled independently of the API tier; the
  API only enqueues and reads state.
- `POST /generations` responds in <500ms plus the topic check, bounded by
  `DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS` (otherwise it only enqueues). `GET /generations/{jobId}`
  responds in <200ms and is cheap enough to poll every 2s per client — back it with the store
  and cache, never by recomputing.

## Definition of done for the backend

Before the backend is "ready" to replace the mock the mobile app runs against today:

1. All five endpoints implemented per [`api-contract.md`](api-contract.md), returning shapes
   that validate against [`openapi.yaml`](openapi.yaml).
2. Contract tests green in CI for every response, including every error `code`.
3. Idempotency, cancellation and the "failed job is `200`, not 5xx" rule verified by tests.
4. Timeouts, retries and a circuit breaker on every outbound provider call.
5. Per-client rate limiting with `429 RATE_LIMITED` + `retryAfterSeconds`.
6. Content-policy gate on both topic and generated output; no unlicensed media.
7. Structured logs correlated by `jobId`, plus the metrics above.
8. `GET /health` reflecting real dependency status.
9. The mobile app, pointed at the real server with `EXPO_PUBLIC_API_MOCK=false`, runs the full
   generate → poll → preview → import flow end-to-end.

Point 9 is the real acceptance test. Everything before it exists to make it pass reliably.

## CI enforcement

The frontend and packages are already gated hard in CI (`.github/workflows/ci.yml`):
typecheck, ESLint with FSD layering (`eslint-plugin-boundaries`) plus complexity limits, a
Prettier check, tests, a "no JavaScript in source" guard, and an OpenAPI lint of this folder's
spec that also fails if the generated types drift from it.

The backend is gated by three jobs in the same workflow: `backend` (points 1–8), `backend-audit`
and `secrets` (point 9). Every point runs through a `make` target in `backend/`, so a failure
reproduces locally with the same command CI uses.

**A note on SOLID and CI.** No linter verifies SOLID as a principle — "single responsibility"
and "dependency inversion" are judgements, enforced in code review, not by a tool. What CI
_can_ enforce are the measurable proxies that make SOLID violations expensive and visible.
Treat green CI as necessary, not sufficient; the review still checks the principles
themselves.

### The gate the backend enforces

| #   | Point                | Enforced by                                                                         | CI step / `make` target     |
| --- | -------------------- | ----------------------------------------------------------------------------------- | --------------------------- |
| 1   | Formatter            | `ruff format --check`                                                               | `backend` / `check`         |
| 2   | Linter               | `ruff check` with `select = ["ALL"]`, any finding fails                             | `backend` / `check`         |
| 3   | Types                | `mypy` strict plus `disallow_any_explicit`; suppression guard                       | `backend` / `check`         |
| 4   | Layering             | import-linter, four contracts in `pyproject.toml`                                   | `backend` / `check`         |
| 5   | Complexity budget    | ruff `C901`, `PLR0913`, `PLR0915`, `PLR1702`                                        | `backend` / `check`         |
| 6   | Dead code            | `vulture` over `src/`, configured in `pyproject.toml`                               | `backend` / `check`         |
| 7   | Contract tests       | `tests/transport/*_contract.py` and `test_spec_alignment.py` against `openapi.yaml` | `backend` / `test`          |
| 8   | Coverage floors      | `pytest-cov` over unit and integration tests, `coverage report --fail-under`        | `backend` / `coverage-gate` |
| 9   | Dependency + secrets | `pip-audit` over the locked dependencies; `gitleaks` over the full history          | `backend-audit`, `secrets`  |

1. **Formatter check** — the language's standard formatter, in check mode. No debate about
   style in review.
2. **Linter, warnings are errors** — the strictest sensible ruleset for the language, run with
   zero-tolerance for warnings, same as the frontend's `--max-warnings 0`. Ruff has no warning
   level: every finding fails the build. The only rule families switched off are `D` (docstrings:
   the repo carries no comments), `CPY` (copyright headers) and the two rules that conflict with
   the formatter (`COM812`, `ISC001`). Tests additionally allow `assert` and magic values.
3. **Static types / strict analysis** — the strictest mode, no escape hatches. mypy runs with
   `strict`, `disallow_any_explicit` and `disallow_any_unimported`. `make guard` fails, case-insensitively, on any
   `# type: ignore`, `# mypy: ignore-errors`, `# noqa`, `# ruff: noqa` or `# pragma: no cover`
   under `src/`, `tests/` and `migrations/`: the repo allows no comments, so a justification for
   a suppression cannot exist, and the fix goes in the code.
4. **Layering / dependency direction** — the SRP + DIP proxy. Four import-linter contracts: the
   layer order (entrypoints → transport | infrastructure → application → domain), domain and
   application free of I/O, HTTP and SDK packages, a pure domain (no clock, randomness,
   serialisation or I/O modules), and transport reaching infrastructure only through
   application ports. A domain module importing an HTTP or SDK type is a build failure.
5. **Complexity budget** — the OCP/SRP proxy. The frontend budget is 12 cyclomatic / 80 lines /
   4 params / 4 deep (`frontend/eslint.base.mjs`). The backend mirrors it in ruff:

   | Limit                 | Frontend | Backend         | Rule      |
   | --------------------- | -------- | --------------- | --------- |
   | Cyclomatic complexity | 12       | 12              | `C901`    |
   | Parameters            | 4        | 4               | `PLR0913` |
   | Nesting depth         | 4        | 4 nested blocks | `PLR1702` |
   | Function size         | 80 lines | 40 statements   | `PLR0915` |

   Function size is the one deliberate deviation. Ruff has no physical-line cap, and a line count
   would reward formatter wrapping: the ruff formatter breaks a call over several lines at 110
   columns, so the same function grows or shrinks with its arguments. Statements are
   format-independent, and in this codebase a function averages about 1.6 formatted lines per
   statement, so 40 statements is about 65 lines. The count includes nested functions, as the frontend's does. `PLR1702` is a preview
   rule, so `pyproject.toml` enables it explicitly with `explicit-preview-rules` instead of
   turning on preview for the whole rule set. `PLR0911` (returns) and `PLR0912` (branches) stay
   at their ruff defaults. There is no file-length cap on the backend; the frontend's 300-line
   cap has no ruff equivalent.

6. **Dead code / unused exports** — `F401`, `F811` and `F841` (via `ALL`) catch unused imports
   and variables; `vulture` over `src/` catches unused functions, classes, methods and
   attributes at 60% confidence. Its false positives are all framework wiring, and are listed
   in `[tool.vulture]` rather than hidden: route handlers (`@router.*`), pydantic validators,
   `model_config`, the `create_app` factory, `HTMLParser` hooks, the arq `WorkerSettings`
   attributes, and the `AUDIO` media kind (a domain value used only by tests until audio notes
   exist). Vulture runs on `src/` only, so code reachable only from tests still fails.

   **Known limitation:** `ignore_names` is global. Vulture has no per-file suppression, and
   inline `noqa` is banned by `make guard`, so a real dead function, method or variable that
   shares one of those names is not reported. The use cases stored on `app.state` used to be on
   the list under their own names (`create_generation`, `get_generation` and so on, the same
   names as the route handlers). They are now stored and read back through the `*_STATE`
   constants in `transport/dependencies.py`, so vulture sees the use and the names are no longer
   suppressed. The remaining entries are names a framework or a base class calls and that nothing
   in `src/` would plausibly define twice: `model_config`, `create_app`, `handle_starttag`,
   `handle_endtag`, `handle_data`, `functions`, `cron_jobs`, `on_startup`, `on_shutdown`,
   `allow_abort_jobs` and `AUDIO`. Each was checked by removing it: it hides exactly its
   framework false positives and nothing else. A new name goes in the ignore list only when it is
   called by a framework by name and cannot be made visible to vulture by passing it through a
   constant, and the audit above is repeated when it does. Revisit `AUDIO` once audio notes
   exist and `src/` uses it.

7. **Contract tests against `openapi.yaml`** — every response the service can emit, success and
   every error `code`, validated against this folder's spec. Each operation has a
   `tests/transport/test_*_contract.py` that validates every declared status and body against the
   spec schemas. `tests/transport/test_spec_alignment.py` closes the gaps a per-endpoint test
   cannot see: the service serves exactly the operations the spec declares, the success status
   of each route is a declared response, and the `ErrorCode` enum equals the spec's. The
   cross-cutting `ROUTE_NOT_FOUND`, `METHOD_NOT_ALLOWED` and `INTERNAL_ERROR` problems are
   tested and validated against the `Problem` schema, but the spec declares them only as codes,
   not as a response on each operation.
8. **Test coverage threshold** — branch coverage over the unit and integration tests together
   (`make test`, then `make test-integration`, then `make coverage-gate`):

   | Scope                                              | Floor | Measured when set |
   | -------------------------------------------------- | ----- | ----------------- |
   | Whole package                                      | 95%   | 98.8%             |
   | `domain` + `application`                           | 98%   | 99.8%             |
   | `infrastructure`, `transport`, `worker`, `main.py` | 90%   | 98.4%             |

   The floors sit a few points under what the suite measures, so a refactor does not trip them
   but a skipped test file or an untested module does. The adapters get the lower bar because
   their remaining lines are defensive branches around real I/O failures that only a live
   dependency produces. Coverage is a floor, not a goal; do not chase 100%.

9. **Security / dependency audit** — `make audit` exports `uv.lock` with hashes and runs
   `pip-audit` (version pinned in the Makefile) against the PyPI advisory data, so a known-
   vulnerable package fails the build. The `secrets` job runs `gitleaks` (version and sha256
   pinned in the workflow) over the full git history with `.gitleaks.toml`, which extends the
   default rules and allowlists only the `generic-api-key` rule under `backend/tests/` (random
   UUIDs and fake keys used as fixtures; provider-specific rules such as AWS or GCP still apply
   there). `.gitleaksignore` holds the one historical finding, a fake test key since replaced.
   GitHub's own secret scanning is a separate platform feature and stays on.

Dependencies added for this gate: `pytest-cov` and `vulture` (dev group, locked in `uv.lock`),
`pip-audit` (run through `uvx`, pinned, not in the lock) and the `gitleaks` binary (CI only).

### What stays in review, not CI

These are the SOLID judgements a tool cannot make. They belong on the pull-request checklist:

- Does each unit have one reason to change, or is a "god service" forming? (S)
- Does a new note type / stage extend via registration, or edit a shared switch? (O)
- Do fakes and real adapters honour the same contract — same errors, same timeouts? (L)
- Are ports shaped by what the application needs, not by the provider SDK? (I)
- Is anything constructing a provider client or opening a connection inside domain/application
  code instead of receiving it? (D)

A change can pass every automated gate above and still fail these. That is expected — the
automation catches the mechanical decay, the review catches the design.
