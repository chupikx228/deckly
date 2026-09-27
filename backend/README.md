# Deckly generation service

FastAPI service that turns a topic into reviewable notes. The contract, architecture and testing
rules live in [`../.claude/backend/`](../.claude/backend/) — read `handoff.md` first.

## Layout

```
src/deckly/
  main.py            composition root: builds the FastAPI app, wires adapters, registers handlers
  config.py          settings from the environment (pydantic-settings), fail-fast on anything missing
  transport/         HTTP: routers, Problem (RFC 9457) model, the single error mapper
  application/       use cases and the ports they depend on; application errors
  domain/            pure business rules; domain errors
  infrastructure/    adapters: async SQLAlchemy + asyncpg, Arq/Redis, JSON logging, web search and
                     source parsing (Tavily), the LLM card generator (Anthropic or DeepSeek) and
                     the retry/circuit-breaker layer
  worker/            Arq worker entrypoint, WorkerSettings and its composition root
migrations/          Alembic (async)
```

Dependencies point inward only. `make check` runs import-linter, which fails the build if
`domain` or `application` import an outer layer or an I/O/SDK package (FastAPI, SQLAlchemy,
Redis, Arq…), or if `transport` reaches infrastructure directly. Ruff's `TID251` (banned-api,
scoped to `src/deckly/domain/` in `pyproject.toml`) also fails it if domain code reads the clock
(`datetime.now()`, `utcnow()`, `today()`) or generates ids (`uuid4()`, `uuid1()`) instead of
receiving them as arguments.

## Errors

Domain and application code raise typed errors (`domain/exceptions.py`,
`application/exceptions.py`). `transport/error_handlers.py` is the only place that turns them
into `application/problem+json`, using the `code` table from `api-contract.md`. Request
validation failures become `400 VALIDATION_FAILED`; anything unmapped is logged with its
traceback and returned as `500 INTERNAL_ERROR` with no internal detail.

## Running locally

Requires [uv](https://docs.astral.sh/uv/) and Docker. Run everything from `backend/`.

```bash
make install        # uv sync --locked (installs Python 3.12 if needed)
cp .env.example .env
make up             # Postgres on :5433, Redis on :6380
make migrate        # alembic upgrade head
make run            # uvicorn on :8000 with reload
```

Every setting is required; the app refuses to start if one is missing, blank or invalid, and
if Postgres or Redis are unreachable. The database URL must use `postgresql+asyncpg`.

The worker (`make worker`) consumes the queue that `POST /v1/generations` fills and runs
`application/pipeline.py` (`RunGeneration`): planning → retrieving sources → parsing →
generating cards → fetching media (only when `includeImages` is set) → finalizing. Every stage
change goes through `JobStore.update`, which row-locks the job, so a cancel that lands first
makes the worker stop at its next stage instead of racing it. It runs separately from the API
and shares nothing with it but Postgres and Redis.

Arq's Redis pool has no read timeout of its own, so the API builds its pool itself
(`infrastructure/queue.py`, `create_queue_pool`) with the same connection settings Arq would use
plus a read timeout, and gives every command it sends through the queue that long to finish,
both from `DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS`. The startup check pings Redis through that same
pool and retries `DECKLY_REDIS_CONNECT_RETRIES` times, one second apart, as Arq does, so a Redis
that accepts connections but never answers makes the API fail to start instead of hanging it.
The read timeout also matters for the enqueue, which runs a `WATCH` transaction: if Redis
answers the `WATCH` and then freezes, redis-py's cleanup reconnects to send `UNWATCH`, and
without a read timeout that wait would never end. An enqueue that gets no answer in time fails
`POST /v1/generations` with `500`, as an unreachable Redis does, instead of hanging it. The job
is already stored by then, so replaying the request with the same `Idempotency-Key` enqueues it
again. When that cleanup gives up, redis-py only lets go of the connection once it is
garbage-collected, which asyncio logs as `Unclosed client session`.

`GET /v1/health` checks Postgres (`SELECT 1` through the API's engine) and Redis (`PING`
through the queue pool) concurrently and reports `degraded` if either fails or does not answer in
time: `DECKLY_DATABASE_HEALTH_CHECK_TIMEOUT_SECONDS` for Postgres, and
`DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS`, the same bound every queue command gets, for Redis
(`infrastructure/health.py`). A plain timeout around the Postgres query is not enough: when a
pooled connection freezes mid-query, cancelling it makes asyncpg send a cancel request and wait,
with no timeout, for the server to acknowledge it, and SQLAlchemy's cleanup waits on that. So
each check runs as its own task that the request only waits on. When the wait runs out, the task
is cancelled once and left to clean up in the background, and until it has, further health
checks answer `degraded` at once instead of starting another. Cancelling it a second time would
make SQLAlchemy lose track of the connection and shrink the pool for good. Concurrent health
requests share one check, so an unauthenticated flood of them holds at most one pooled
connection. The model, search and image providers are not checked (see the contract).

Every job store call, in the API and in the worker, goes through the same kind of bound
(`BoundedJobStore` in `infrastructure/job_store.py`), with `DECKLY_DATABASE_JOB_STORE_TIMEOUT_SECONDS`
for the whole call: taking a pooled connection, the queries and the commit. When Postgres freezes,
`POST /v1/generations`, `GET /v1/generations/{jobId}` and the cancel fail with `500` in that time,
as an unreachable Postgres does, instead of hanging. Neither of asyncpg's own timeouts covers this.
Postgres's `statement_timeout` is enforced by the server, so it never fires when the server's
answers do not arrive at all. asyncpg's `command_timeout` does raise on time, but it leaves the
connection waiting for the cancel to be acknowledged, and the `ROLLBACK` SQLAlchemy sends next
(already during the pool's pre-ping) waits on that with no timeout. So each call runs as its own
task. When the wait runs out it is cancelled once and left to roll back in the background, as the
health check does. The cancel lands before the commit, so a call that timed out stores nothing
unless the freeze came while the commit itself was in flight. In that case the create has still
created the job, and replaying the request with the same `Idempotency-Key` returns it. A cancel
that timed out that way has already cancelled the job, so a retry gets `409`. Calls left
cleaning up each hold a pooled connection until Postgres answers again, so a long freeze can
hold at most the whole pool. Further calls then time out while waiting for a connection, rather
than opening more.

A cancel also stops the work already in flight. Once the job is stored as cancelled,
`POST /v1/generations/{jobId}/cancel` leaves an Arq abort marker for it, and the worker
(`allow_abort_jobs`) cancels the job's task at its next poll, within about half a second. The
cancellation reaches the model call itself and closes its HTTP request, so the provider is not
left producing a reply nobody will read; the resilience layer neither retries it nor counts it
against the provider, and the job stays `cancelled`. The marker is only written after the
cancel is committed: the other way round, the worker would record the interrupted job as
`failed` and the cancel would then get a `409`. Sending it is best effort. If Redis cannot be
reached or does not answer in time, `job_abort_not_signalled` is logged, the cancel still
answers `204`, and the worker stops at its next stage as before. Arq removes a marker when it
cancels or finishes the job; a marker written for a job that ended at that same moment stays in
`arq:abort`, which is harmless because the stored job is already terminal.

All four provider ports are real (see below) and wired in `worker/settings.py` (`startup`).

## Sources

`infrastructure/search/` implements the `SourceRetriever` and `SourceParser` ports. The retriever
makes one Tavily `POST /search` per job (raw `httpx2`, `DECKLY_PROVIDER_SEARCH_*`) for the topic,
asking for the page text Tavily has already extracted (`include_raw_content: "text"`), so the
worker never fetches arbitrary URLs itself. The request language is passed as a ranking hint only
when its primary subtag is a two-letter ISO 639-1 code. Each result becomes a page whose `Source`
is that result's own title and URL, stamped with the time the answer arrived; a result whose URL
is not `http(s)` or whose title is blank once cleaned is dropped, a repeated URL is kept once, and
at most `DECKLY_PROVIDER_SEARCH_MAX_RESULTS` pages are kept even if the provider returns more.
When Tavily has no page text, its snippet of that same page stands in.

The parser cleans every page before the card generator numbers it. NUL, lone surrogates, control
and format characters (bidi overrides, zero-width spaces) are removed from the text and the title,
except the zero-width joiner and non-joiner that some scripts and emoji need; whitespace is
collapsed, titles become one line of at most 200 characters, and anything shaped like the
prompt's `<source>` / `</source>` markers is defused so page text cannot open or close a source
block. Text is cut at a word boundary at `DECKLY_PROVIDER_SEARCH_MAX_SOURCE_CHARACTERS`. A page is
dropped, never failing the job, when more than a tenth of it is replacement, control,
private-use or unassigned characters (binary content), when fewer than 50 visible characters are
left, or when it repeats an earlier page's text. The work runs in a thread so a large page does
not stall the worker's event loop.

The search call goes through the same retry/circuit-breaker layer as the model, with its own
breaker and policy. Timeouts, network errors, `408`/`409`/`429` and `5xx` are retried with jittered
backoff within `DECKLY_PROVIDER_SEARCH_DEADLINE_SECONDS`, then fail the job
`PROVIDER_UNAVAILABLE`, as does an exhausted Tavily plan or spending limit (`432`/`433`). Those
are not retried but do count toward the search circuit breaker, so a quota that stays exhausted
opens it and later jobs fail fast without calling Tavily until the reset probe succeeds. Any
other rejection (`400`, `401`, `403`…) or a body that is not the expected JSON is
not retried and fails the job `GENERATION_FAILED`. A search with no usable page is not an error
here: the parser returns no material, the card generator skips the model call, and the job ends
`NO_VALID_CONTENT`. Settings refuse to load unless the search, model and media deadlines together
leave room within `DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS`.

## Card generation

`infrastructure/card_generator/` implements the `CardGenerator` port with one model call per
job. `DECKLY_PROVIDER_MODEL_PROVIDER` picks the client: `anthropic` (official SDK) or `deepseek`
(raw `httpx2` against its OpenAI-compatible `/chat/completions`, JSON output mode). The model
never gets to invent a source: the prompt numbers each `SourceMaterial`, notes cite those
numbers, and the adapter maps them back to the material's own `Source`. With no material the
model is not called.

The model's reply is untrusted. The adapter extracts JSON from prose, code fences or a reply cut
off at the token limit. A reply split into several top-level objects, such as `{"deck": …}`
followed by `{"notes": […]}`, is merged: the first deck wins and the notes are concatenated in
order. It then validates every note through the domain: a note of an
unrequested or unknown type, with fields that do not exactly match its type, an unrepairable
cloze, invalid distractors or no citable source is dropped, and NUL characters and lone
surrogates are stripped from every string first. Cloze numbering with gaps is renumbered from 1.
A note that asks the same thing as an earlier valid note of the same type is dropped as a
duplicate, keeping the first, before the cap, so a repeated reply does not use up `cardCount`
twice. What a note asks is its front and back for the basic types, its text for cloze, and its
question, answer and set of distractors, in any order, for multiple choice. `addReverse` and a
cloze's `extra` do not make two notes different, and a `basic_reversed` note with its sides
swapped is not a duplicate. At most `cardCount` notes are returned, never padded. A reply that
yields no note ends the job `NO_VALID_CONTENT`; a reply is never retried for its content.
`image_occlusion` is not generated here, because a valid note needs a licensed image the media
stage has to supply, and a request for it without `includeImages` is rejected up front.

Duplicates, and a distractor that repeats the answer or another distractor, are found with one
comparison (`domain/text.py`, `normalise_for_comparison`). It ignores case, fullwidth and
halfwidth forms, how an accent is encoded (`é` or `e` plus a combining accent), runs of
whitespace and invisible characters. It does not fold other compatibility forms: `x²`, `x₂` and
`x2`, `Straße` and `Strasse`, or a ligature and its letters stay different. Case is folded one
character at a time (Unicode simple case folding), which is why `ß` is not turned into `ss`.
The price of ignoring case is that `Polish` and `polish` count as the same.

Every call goes through `infrastructure/resilience.py`: a timeout per attempt
(`MODEL_TIMEOUT_SECONDS`), retries with exponential backoff and full jitter for transient
failures only (network errors, timeouts, 408/409/429/5xx, honouring `retry-after`), an overall
`MODEL_DEADLINE_SECONDS` after which no new attempt starts, and a circuit breaker per worker
process that opens after `MODEL_CIRCUIT_FAILURE_THRESHOLD` consecutive transient failures,
leaving out timeouts of oversized requests (see below). An outage or an open circuit ends the
job `PROVIDER_UNAVAILABLE`; a rejected request (bad key, unknown model) ends it
`GENERATION_FAILED`. Keep the deadline below
`DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS` minus the time the earlier stages need, or Arq
kills the job first and it ends `GENERATION_FAILED` instead, and keep
`MODEL_MAX_OUTPUT_TOKENS` small enough to be produced within one attempt's timeout, so a
large deck is cut at the token limit and its complete notes are kept rather than lost to a
timeout.

A slow oversized request is not a sign the provider is down, so the breaker does not treat its
timeouts like a small request's. The expected output of a request is estimated from its
`cardCount`, at 100 tokens per note plus 100 for the deck (`card_generator/prompt.py`). A request
expected to need at least half of `MODEL_MAX_OUTPUT_TOKENS` is oversized
(`llm/resilient.py`), and its timeouts neither count toward the threshold nor reset the count;
a recovery trial that times out that way lets the next call try again. Its other transient
failures (network errors, 408/409/429/5xx) still count, since its size does not explain them.
The reasoning: generation time grows with the tokens produced, and the budget is meant to fit
one attempt. A request needing less than half of it that still times out means the provider is
running at under half its expected speed, which is an outage signal. Above half, ordinary
latency swings can push a healthy provider past the timeout, and one slow 200-card deck must
not open the circuit and fail every other job. With 16 000 tokens, decks of about 80 cards or
more are oversized; the app's wizard asks for at most 50. The estimate only classifies: every
request still asks for the whole `MODEL_MAX_OUTPUT_TOKENS`. A timeout while connecting cannot be
told apart from a slow reply, so for an oversized request it is not counted either.

## Note regeneration

`POST /v1/notes/regenerate` runs in the API process, synchronously: no job, no queue, nothing
cached or stored. `application/regeneration.py` checks the client's regeneration budget, runs one
web search for the topic, parses it with the same `CleaningSourceParser` as a job, and asks the
model for one note of the requested type (`card_generator/regenerator.py`). The rejection
`reason` picks a line of guidance in the prompt; the rejected note's fields are shown to the model
as JSON data, cut at 2000 characters, with `<` and `>` escaped so they cannot open or close a
prompt block, and NUL and lone surrogates stripped like the rest of the prompt. The reply goes
through the same extraction and the same per-note validation as a job (`NoteDrafter`): wrong
type, mismatched fields, no citable source or a repeat of the rejected note are dropped, and the
first note that survives is returned. None surviving is `503 NO_VALID_CONTENT`.

The call must answer within `DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS` (10). Each step has its
own `DECKLY_REGENERATE_*` timeout, separate from the job settings: the rate-limit check in Redis
(0.5 s), the search (2.5 s) and the model (5.5 s). Settings refuse to load unless those three
plus 1.5 s for parsing, validation and the response fit within the total. Search and model get
**one attempt each, no retry**: a second attempt after a timeout cannot fit in what is left, and
one that follows an instant `429`/`5xx` is better left to the user tapping again, told when by
`retryAfterSeconds`. Timeouts, transient errors and an open circuit are `503
UPSTREAM_UNAVAILABLE`. Each step already has its own timeout; on top of that, the whole use
case runs under `asyncio.timeout` of the total, which cancels whatever is still running and
answers 503. Search and model calls go through their own circuit breakers in the API process,
configured with the job's `DECKLY_PROVIDER_*_CIRCUIT_*` values; breaker state is per process, so
it is not shared with the worker's. The model's output is capped at
`DECKLY_REGENERATE_MODEL_MAX_OUTPUT_TOKENS`, and the search keeps at most
`DECKLY_REGENERATE_SEARCH_MAX_RESULTS` pages of `DECKLY_REGENERATE_SEARCH_MAX_SOURCE_CHARACTERS`,
so the prompt stays small enough for a quick reply.

The budget is a fixed-window counter in Redis per `X-Client-Id`
(`infrastructure/rate_limit.py`, `DECKLY_LIMIT_NOTE_REGENERATIONS_PER_WINDOW` per
`DECKLY_LIMIT_NOTE_REGENERATION_WINDOW_SECONDS`), separate from the job quota. If Redis cannot
be reached, regeneration fails closed with 503 rather than running unmetered.

## Media

`infrastructure/media/` implements the `MediaFetcher` port against the Wikimedia Commons API (raw
`httpx2`, `DECKLY_PROVIDER_MEDIA_*`, no API key). When `includeImages` is set, the card
generator's prompt offers each note an optional `"image"` key: a short English phrase naming what
a picture of the note would show. The adapter keeps it only as a search phrase for a note that
survived validation, cleaned, at most 100 characters, and ignores it otherwise; a bad phrase never
costs the note. Commons wants a User-Agent it can reach its owner through, so set
`DECKLY_PROVIDER_MEDIA_USER_AGENT` to a real URL or email address; a `403` is treated as a block.

The media stage searches once per illustrated note, for at most `MEDIA_MAX_IMAGES` notes, with at
most `MEDIA_MAX_CONCURRENCY` searches at a time. The phrase is reduced to plain lowercase words
first, so it cannot use CirrusSearch operators (`insource:`, `-`, `AND`/`OR`/`NOT`, wildcards,
regexes), cut at a word to the 300 characters CirrusSearch accepts, and the search is limited to
bitmap and drawing files. Each note gets the first
candidate in search rank order that passes every check below, and a file already given to an
earlier note is skipped:

- **Licence.** The file's machine-readable `License` code must be `cc0` or `pd`, its
  `AttributionRequired` flag must be exactly `false`, and its `Restrictions` (personality rights,
  trademark, insignia) must be empty. The contract's `Media` has no field for an author or a
  source link, so licences that require attribution (CC BY, CC BY-SA) are dropped, not guessed.
  The licence is returned as `CC0-1.0` or `Public-Domain`.
- **URL.** The Commons-scaled thumbnail (`MEDIA_THUMBNAIL_WIDTH`, which Commons rounds to one of
  its standard widths; SVG comes back as PNG) must be `https` on `wikimedia.org` or one of its
  subdomains, with no credentials or port. These URLs are unsigned, need no authentication and
  stay valid until the file is deleted or renamed on Commons, which is how the 24-hour
  requirement is met. Wikimedia gives no formal guarantee.
- **Picture.** A displayable image type, with both thumbnail sides at least 200 pixels.
- **Alt text.** The file's description with markup removed, cut at a word to 250 characters, or
  the file name when the description is blank. A file with neither is dropped. The description is
  usually English and may not match the card language.

A note with no passing candidate simply has no image. The media stage is a bulkhead: every
search goes through the resilience layer with its own breaker and policy, and a failed search
(outage, timeout, `429`/`5xx`, an open circuit, a block, a malformed answer) only costs that note
its image. The whole stage stops at `MEDIA_DEADLINE_SECONDS` and keeps what it found by then. The
pipeline also catches anything that still escapes the adapter, logs `media_skipped` (warning for
an outage, error with the traceback for anything else) and succeeds with the generated notes as
they were. An image outage therefore never fails a job and never produces `PROVIDER_UNAVAILABLE`.
The port returns attachments keyed by `clientId`, never notes, so a media adapter cannot drop or
duplicate a note; attachments for unknown notes are ignored and a repeated `mediaId` is kept once.

## Checks

```bash
make check          # ruff format --check, ruff check, mypy --strict, import-linter
make fix            # ruff format + ruff check --fix
make test           # pytest, no infrastructure needed
make test-integration  # Postgres + Redis tests; needs make up && make migrate
make test-live     # calls the model provider configured in .env; needs a real key, costs money
```

CI runs `make install`, `make check`, `make test`, then `make migrate` and `make test-integration`
against Postgres and Redis service containers (`.github/workflows/ci.yml`, `backend` job).
