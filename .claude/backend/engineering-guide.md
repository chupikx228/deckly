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

- The generation use case depends on `SourceRetriever`, `CardGenerator`, `MediaFetcher`,
  `JobStore` **interfaces**, defined in the inner layers. The concrete provider clients
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

## Idempotency

`POST /generations` carries a client-generated `Idempotency-Key`.

- Replaying a request with the same key returns the **original** job, not a second one. Store
  `key → jobId` and look it up before enqueueing.
- Scope the key to the client (`X-Client-Id`) so two clients cannot collide.
- The client relies on this to survive network retries without spawning duplicate jobs. Treat
  it as a correctness requirement, not an optimization.

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
  an expired URL is worse than a miss.

## Rate limiting and abuse

- Rate-limit per `X-Client-Id`. Generation is expensive; one client must not exhaust the
  budget. Over the limit → `429 RATE_LIMITED` with `retryAfterSeconds`.
- `X-Client-Id` is an anonymous device id, not an identity — it is trivially spoofable. Use it
  for shaping, and add coarse IP-based limits as a backstop. Do not make security decisions on
  it alone.

## Content safety

- Reject topics that violate policy up front → `422 TOPIC_REJECTED`.
- Filter **generated** content for policy violations before it reaches the client. The model's
  output is untrusted; a topic passing the gate does not mean every generated card is safe.
- `sources` exist so the user can verify claims — they are a product feature, not decoration.
  Every returned note carries at least one real, reachable source; a note without one is
  dropped.
- Media without a known `license` must not be returned at all. Licensing is a legal
  requirement, not a nice-to-have.

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
- `POST /generations` responds in <500ms (it only enqueues). `GET /generations/{jobId}`
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

The backend does not exist yet, so its job in that workflow is a **placeholder**: it passes
while there is no backend directory and fails the moment backend code appears without its own
gate wired up. When you add the service, replace that placeholder with the checks below,
running in the backend's own toolchain.

**A note on SOLID and CI.** No linter verifies SOLID as a principle — "single responsibility"
and "dependency inversion" are judgements, enforced in code review, not by a tool. What CI
_can_ enforce are the measurable proxies that make SOLID violations expensive and visible.
Treat green CI as necessary, not sufficient; the review still checks the principles
themselves.

### The gate the backend must add

1. **Formatter check** — the language's standard formatter, in check mode. No debate about
   style in review.
2. **Linter, warnings are errors** — the strictest sensible ruleset for the language, run with
   zero-tolerance for warnings, same as the frontend's `--max-warnings 0`.
3. **Static types / strict analysis** — if the language is typed, the strictest mode, no
   escape hatches (no untyped `any`-equivalent, no ignored errors without a justification).
4. **Layering / dependency direction** — the SRP + DIP proxy. Enforce that dependencies point
   inward (transport → application → domain, never the reverse) and that infrastructure is
   reached only through ports, with an import-boundary tool (an `import-linter`-style contract,
   the backend equivalent of `eslint-plugin-boundaries`). A domain module importing an HTTP or
   SDK type is a build failure, not a review nit.
5. **Complexity budget** — the OCP/SRP proxy. Cap cyclomatic complexity per function, function
   length, parameter count, and nesting depth (the frontend uses 12 / 80 lines / 4 params / 4
   deep). A function over budget is where responsibilities pile up; fail the build and make the
   author split it.
6. **Dead code / unused exports** — keep the surface honest, but tune it: barrel/public-API
   files produce false positives, so scope it rather than shipping a noisy gate.
7. **Contract tests against `openapi.yaml`** — every response the service can emit, success and
   every error `code`, validated against this folder's spec. This is the cheapest insurance
   that the mobile app will accept the payloads. Non-negotiable.
8. **Test coverage threshold** — a floor (e.g. 80%) that fails the build when it drops, with
   the domain and application layers held higher than the adapters. Coverage is a floor, not a
   goal; do not chase 100%.
9. **Security / dependency audit** — a vulnerability scan of dependencies and a secret scan, so
   a leaked key or a known-vulnerable package fails the build.

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
