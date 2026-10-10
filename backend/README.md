# Deckly generation service

FastAPI service that turns a topic into reviewable notes. The contract, architecture and testing
rules live in [`../.claude/backend/`](../.claude/backend/) — read `api-contract.md` first, then
`engineering-guide.md`.

## Layout

```
src/deckly/
  main.py            composition root: builds the FastAPI app, wires adapters, registers handlers
  config.py          settings from the environment (pydantic-settings), fail-fast on anything missing
  transport/         HTTP: routers, Problem (RFC 9457) model, the single error mapper
  application/       use cases and the ports they depend on; application errors
  domain/            pure business rules; domain errors
  infrastructure/    adapters: async SQLAlchemy + asyncpg, Arq/Redis, JSON logging, web search and
                     source parsing (Tavily), the LLM card generator (Anthropic, DeepSeek or
                     Gemini) and the retry/circuit-breaker layer
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
connection. The model, search and image providers are not checked (see the contract). When the
request carries `X-Client-Id`, the client's quota is read at the same time, under the same Redis
bound; if that read fails, the quota is left out and the status is `degraded` (see "Generation
quota" below).

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

## Generation quota

`POST /v1/generations` spends one unit of a per-client daily budget
(`DECKLY_LIMIT_GENERATION_JOBS_PER_DAY`, keyed by `X-Client-Id`) and one unit of a coarser
per-address budget (`DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY`, which settings refuse to
load below the per-client limit). Both are counters in Redis (`infrastructure/quota.py`).

**Window.** Both counters use fixed windows aligned to the UTC day, the same kind of window as
the regenerate limiter (`infrastructure/rate_limit.py`, whose key and window helpers the quota
shares). The contract once said "rolling day", but a sliding 24 hours has no single moment the
budget comes back, and the response has one `resetsAt`. A window that starts at a client's first
job would have one, but before that job `GET /v1/health` would have to report a `resetsAt` that
moves on every read. With UTC days, `resetsAt` is always the next 00:00Z, a 429's
`retryAfterSeconds` counts down to it, and every read agrees. The cost, already accepted for
regenerate, is that a client can start up to twice the limit across midnight.

**Reserving.** A Lua script checks both counters and increments both only if neither is at its
limit, so concurrent requests cannot overshoot and a refused request uses nothing. Each counter
expires a day after its last increment, by which time its window is over. `CreateGeneration`
first looks the `Idempotency-Key` up (`JobStore.find`). A replay returns its job and the current
quota without reserving, so the retry that follows a lost `202` gets its job back even when that
job used the last unit. Only a request with an unused key reserves, before the job is stored.
A refused reservation looks the key up once more before answering `429`: a double tap sends two
requests with one key a few milliseconds apart, and when the first took the last unit, the second
gets the first's job rather than a `429` until midnight.
If storing it fails, or a concurrent request with the same key stored its job first, the unit is
given back by a second script that decrements each counter only while it is above zero, so it
can never leave behind a negative counter or a key with no expiry. A job that was stored but
failed to enqueue keeps its unit; its replay enqueues it without reserving again.

Three edge cases make the count drift by one, and are accepted rather than fixed. If Redis stops
answering between the reservation and the give-back, the give-back is logged
(`generation_quota_not_released`) and dropped rather than hiding the error that caused it, so a
unit stays spent on no job until midnight. A reservation held up in a stalled network can reach
Redis after the request has already answered `503`, with the same effect. The other way round, if
a job store call times out but its commit still lands (see the job store bound above), the unit
was already given back, so that one job is free.

**Address.** The address is the connection's peer, `request.client.host`. The app does not read
`X-Forwarded-For` itself: uvicorn rewrites the peer from that header only for connections from
`--forwarded-allow-ips`, which defaults to `127.0.0.1`. There is no reverse proxy in this
deployment yet. If one is added, pass its address in `--forwarded-allow-ips`; otherwise every
request is counted against the proxy's address. IPv6 peers are counted per /64, since one host
usually holds the whole prefix and could otherwise use a new address for every request.
IPv4-mapped IPv6 addresses count as the IPv4 address. A connection with no peer address, which
uvicorn never produces over TCP, is counted under one shared `unknown` address rather than not
at all.

**Redis down.** Every quota command runs under `DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS`, the bound
every other queue command gets. When the reservation cannot finish in that time, the request is
refused with `503 UPSTREAM_UNAVAILABLE` before anything is stored, rather than let through
unmetered. The quota lives in the same Redis as the job queue, so a request let through would
fail at the enqueue a moment later anyway, with its job already stored. This matches the
regenerate limiter. `GET /v1/health` instead reports `degraded` and leaves the quota out.

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
`NO_VALID_CONTENT`. Settings refuse to load unless the search, model, media, media judge and
moderation deadlines together leave room within `DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS`.

## Card generation

`infrastructure/card_generator/` implements the `CardGenerator` port with one model call per
job. `DECKLY_PROVIDER_MODEL_PROVIDER` picks the client: `anthropic` (official SDK), `deepseek`
(raw `httpx2` against its OpenAI-compatible `/chat/completions`, JSON output mode) or `gemini`
(raw `httpx2` against `models/{model}:generateContent`, JSON output mode). The same choice
drives every model call: card generation, topic and content moderation, and note regeneration.

For Gemini, set `DECKLY_PROVIDER_MODEL_BASE_URL=https://generativelanguage.googleapis.com/v1beta`
and Gemini 3.x model names in `DECKLY_PROVIDER_MODEL_NAME` and
`DECKLY_PROVIDER_MODERATION_MODEL_NAME`. The key travels in the `x-goog-api-key` header, never in
the URL. Every request asks for `thinkingLevel: low`, because Gemini 3 thinks at `high` by
default and thinking tokens count against `maxOutputTokens` and the attempt timeout; the
deprecated 2.5 models reject that field. A prompt blocked by Gemini's own filter
(`promptFeedback.blockReason`) or a candidate stopped for `SAFETY`, `RECITATION`, `LANGUAGE`,
`OTHER`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `ESCALATION` or an image-safety reason counts
as a refusal, so moderation fails closed; `PUP_LIMITED_DISABLED` (a suspended account) is a
rejected request instead, so it shows up as a failure rather than as every topic being blocked.
A `429` without a `retry-after` header honours the `google.rpc.RetryInfo` delay in its body. The model
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
`image_occlusion` is not generated at all yet: `GenerationRequest` rejects any request that names
it, so it never reaches the generator. The decided design, a fixed Gemini vision step that places
the regions during the media stage, is recorded in `.claude/backend/api-contract.md` ("Resolved
decisions", item 5) and is implemented in DEC-59.

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
bitmap and drawing files. The first `MEDIA_JUDGED_CANDIDATES_PER_NOTE` candidates in search rank
order that pass every check below make the note's shortlist, and the image classifier (see
"Content safety") picks from it:

- **Licence.** The file's machine-readable `License` code must be `cc0`, `pd`, or a generic
  `cc-by-<version>` / `cc-by-sa-<version>` (1.0, 2.0, 2.5, 3.0 or 4.0). Ported variants such as
  `cc-by-sa-3.0-de` and every NonCommercial or NoDerivatives licence are dropped. The
  `AttributionRequired` flag must match the licence exactly (`false` for CC0 and public domain,
  `true` for CC BY and CC BY-SA), and `Restrictions` (personality rights, trademark, insignia)
  must be empty. The licence is returned as its SPDX id: `CC0-1.0`, `Public-Domain`,
  `CC-BY-4.0`, `CC-BY-SA-3.0` and so on.
- **Attribution.** CC BY and CC BY-SA files carry an `attribution`, and a file whose attribution
  cannot be derived is dropped, never guessed. The author is the file's `Attribution` credit
  line when the author set one, otherwise its `Artist`, with markup removed; a blank author, one
  made only of the words "unknown" and "author" (`Unknown author.`, `Author unknown`), or one
  longer than 200 characters drops the file. The title is the
  file name, the source is the file's Commons description page (`https` on `wikimedia.org`), and
  the licence URL is the Creative Commons deed from the backend's own table, never the URL
  Commons reports.
- **URL.** The Commons-scaled thumbnail (`MEDIA_THUMBNAIL_WIDTH`, which Commons rounds to one of
  its standard widths; SVG comes back as PNG) must be `https` on `wikimedia.org` or one of its
  subdomains, with no credentials or port. These URLs are unsigned, need no authentication and
  stay valid until the file is deleted or renamed on Commons, which is how the 24-hour
  requirement is met. Wikimedia gives no formal guarantee.
- **Picture.** A displayable image type, with both thumbnail sides at least 200 pixels.
- **Alt text.** The file's description with markup removed, cut at a word to 250 characters, or
  the file name when the description is blank. A file with neither is dropped. The description is
  usually English and may not match the card language.

A note with no passing candidate, or none the classifier accepts, simply has no image. The
classifier ranks the acceptable candidates of every note; each note gets its best-ranked one,
and a file already given to an earlier note falls through to that note's next pick. The media
stage is a bulkhead: every
search goes through the resilience layer with its own breaker and policy, and a failed search
(outage, timeout, `429`/`5xx`, an open circuit, a block, a malformed answer) only costs that note
its image. The whole stage stops at `MEDIA_DEADLINE_SECONDS` and keeps what it found by then. The
pipeline also catches anything that still escapes the adapter, logs `media_skipped` (warning for
an outage, error with the traceback for anything else) and succeeds with the generated notes as
they were. An image outage therefore never fails a job and never produces `PROVIDER_UNAVAILABLE`.
An image classifier outage, or an unreadable reply, is such an escape: the job succeeds without
images and the result is not cached.
The port returns attachments keyed by `clientId`, never notes, so a media adapter cannot drop or
duplicate a note; attachments for unknown notes are ignored and a repeated `mediaId` is kept once.

## Content safety

`infrastructure/moderation/` screens topics and generated content against one content policy
(`moderation/prompt.py`), using the configured model provider with a separate, cheap model
(`DECKLY_PROVIDER_MODERATION_MODEL_NAME`, `claude-haiku-4-5` in `.env.example`). Untrusted text
reaches the classifier as a JSON document, so it cannot break out of its delimiters.

- **Topic check** (`LlmTopicModerator`, wired in `main.py`): runs in `POST /generations` after the
  quota is reserved and before the job is stored. The topic and the `instructions` are judged
  together. A block is `422 TOPIC_REJECTED` and spends the quota unit. There is one attempt,
  bounded by `DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS` (default 2 s, at most 10). A timeout,
  provider error or unreadable verdict is `503 UPSTREAM_UNAVAILABLE` and gives the unit back.
  Published latency for Haiku 4.5 is roughly 0.6–0.8 s to the first token, so the request
  normally takes about 1 s rather than the 500 ms it took before this check.
- **Generated-content filter** (`LlmContentModerator`, wired in the worker): one batched call
  per job after `generating_cards`, before media. A note is kept only on an explicit `"allow"`;
  a missing or unreadable verdict drops it. A deck title that is not allowed is replaced with the
  topic. Retries follow `DECKLY_PROVIDER_MODERATION_FILTER_{TIMEOUT_SECONDS,DEADLINE_SECONDS,
MAX_ATTEMPTS}`; the deadline counts towards the generation job timeout. Results are cached after
  filtering, and a cache hit is not screened again.
- **Image screening** (`LlmCandidateJudge`, wired in the worker): images are found after the
  content filter has run, and Commons titles and descriptions are third-party wiki text, so one
  batched call per job judges every shortlisted candidate's file name, description and (up to
  ten, non-hidden) categories against the note it would illustrate and the phrase it was found
  with. The prompt adds an image policy to the content policy: photographs of nudity, sexual
  activity, graphic injury, gore or a dead body are always blocked, and anatomical diagrams,
  clinical illustrations and artworks only pass when the note's own subject calls for them. A
  candidate is accepted only when its text shows that the picture's main subject is what the
  note needs; this is what keeps the Commons hit for "stop sign" that is a photo of letterboxes
  out of the deck. The reply ranks the acceptable candidates per note, and anything it leaves
  out, misnumbers or omits gets no image. The call is skipped when no note has a shortlisted
  candidate. Retries follow `DECKLY_PROVIDER_MEDIA_JUDGE_{TIMEOUT_SECONDS,DEADLINE_SECONDS,
MAX_ATTEMPTS}`, and the deadline counts towards the generation job timeout. The classifier judges
  text only, never pixels, so a mislabelled file can still get through.

All three checks fail closed: a refusal from the classifier counts as a block (for images: no
images). Circuit-breaker and retry-delay settings are shared with `DECKLY_PROVIDER_MODEL_*`, but
each check has its own breaker, so an image classifier outage never fails a job at the content
filter. `POST /v1/notes/regenerate` is not screened yet (DEC-47).

## Observability

Everything lives in `infrastructure/observability/` and `infrastructure/logging.py`, and is built
once per process (`create_observability`): the API in `create_app`, the worker in `worker/main.py`.
Nothing needs external infrastructure: logs go to stdout, metrics are scraped over HTTP, and traces
are either kept in-process (their ids still land in the logs) or printed to stdout.

### Logs

Every line is one JSON object on stdout (`JsonFormatter`). Event names are
`<subject>_<past-tense verb>` in snake_case (`generation_stage_entered`, `provider_call_finished`),
and field names are snake_case. A filter on the handler adds the correlation fields to every line,
including library lines, unless the call passed the same field itself:

| Field        | Set by                                                                          |
| ------------ | ------------------------------------------------------------------------------- |
| `job_id`     | `RunGeneration` for everything a job does in the worker, explicitly elsewhere   |
| `client_id`  | the HTTP middleware, from `X-Client-Id`, only when it is a canonical UUID v4    |
| `request_id` | `RegenerateNote`, for everything one `POST /v1/notes/regenerate` does           |
| `trace_id`   | the current OpenTelemetry span, so it also links the API line that queued a job |
| `span_id`    | the current span                                                                |

The binding is a context variable (`application/correlation.py`), so adapters never pass the id
by hand and code that runs in another task or thread (`asyncio.to_thread`, the concurrent image
searches) inherits it.

The events that make up one job, in order:

| Event                                                                                                                                       | Where                  | Carries                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | ------------------------------------------------- |
| `http_request_finished`                                                                                                                     | API, every request     | method, route template, status, `duration_ms`     |
| `generation_admitted`                                                                                                                       | API                    | `admission` (`queued` or `replayed`), status      |
| `topic_rejected`, `rate_limit_rejected`                                                                                                     | API                    | `limit` for the rate limit                        |
| `generation_started`                                                                                                                        | worker                 | status, stage                                     |
| `generation_cache_checked`                                                                                                                  | worker                 | `result` (`hit`, `miss`, `corrupt`, `error`)      |
| `generation_stage_entered`                                                                                                                  | worker, every stage    | stage, progress                                   |
| `provider_call_finished`                                                                                                                    | every provider attempt | `operation`, `attempt`, `outcome`, `duration_ms`  |
| `provider_call_retrying`, `provider_call_refused`, `circuit_opened`, `circuit_closed`                                                       | resilience layer       | `operation`                                       |
| `sources_retrieved`, `sources_parsed`, `card_generation_finished`, `content_screened`, `images_screened`, `media_fetched`, `media_attached` | adapters               | counts only                                       |
| `generation_succeeded` / `generation_failed` / `generation_stopped`                                                                         | worker                 | `duration_ms`, failure code, traceback on failure |
| `generation_cancelled`                                                                                                                      | API                    | stage and progress at the time                    |

To follow one job locally:

```bash
make run    2>&1 | tee api.log
make worker 2>&1 | tee worker.log
jq -c 'select(.job_id == "<jobId>") | [.timestamp, .message, .stage // .operation // .result]' api.log worker.log
jq -c 'select(.trace_id == "<trace_id from the lines above>")' api.log   # the POST that queued it
```

`tests/integration/test_observability.py` does exactly this against a full run: it posts a job
through the real app, runs it through the worker entry point with Tavily and Commons answering
over mock HTTP, and asserts the complete ordered list of lines carrying that `job_id`, that they
all share the trace of the `POST`, and that the request and admission lines carry the client.

What is never logged: API keys (redacted from provider error text, and the Anthropic SDK error is
not chained into the traceback because its message is the raw error body), the topic,
`instructions` and generated text (adapters log counts only; the search query is redacted from
Tavily error text), SQL parameters (`hide_parameters=True` on the engine), client IP addresses
(uvicorn's access log is off; `http_request_finished` replaces it), query strings and path values
(the route template is logged instead, and a method outside the standard set is logged as
`_OTHER`), and the URLs HTTP clients request (`httpx`/`httpx2` are
raised to `WARNING`, since Commons search URLs carry search terms taken from generated cards). One
known residue: a Commons API error copies up to 500 characters of the provider's `info` text into
the exception, and that text can echo search terms.

### Metrics

Prometheus format, from `prometheus-client`. The API serves `GET /metrics` on its own port (not
under `/v1`, not in the OpenAPI document); the worker serves the same format on
`DECKLY_OBSERVABILITY_WORKER_METRICS_HOST`:`DECKLY_OBSERVABILITY_WORKER_METRICS_PORT` (default
`127.0.0.1:9464`). Each process has its own registry, so scrape both. A worker whose metrics port
is taken (a second worker on the same host) logs `worker_metrics_unavailable` and runs without
exposing metrics rather than refusing to start.

| Metric                                                                     | Labels                    | Process |
| -------------------------------------------------------------------------- | ------------------------- | ------- |
| `deckly_generation_jobs_admitted_total`                                    | `outcome`                 | API     |
| `deckly_generation_jobs_cancelled_total`                                   |                           | API     |
| `deckly_generation_jobs_finished_total`                                    | `outcome`, `failure_code` | worker  |
| `deckly_generation_job_duration_seconds`                                   | `outcome`                 | worker  |
| `deckly_generation_stage_duration_seconds`                                 | `stage`, `outcome`        | worker  |
| `deckly_provider_call_duration_seconds`                                    | `operation`, `outcome`    | both    |
| `deckly_provider_call_retries_total`                                       | `operation`               | both    |
| `deckly_provider_circuit_rejections_total`, `deckly_provider_circuit_open` | `operation`               | both    |
| `deckly_generation_cache_lookups_total`                                    | `result`                  | worker  |
| `deckly_generation_queue_depth`                                            | `queue`                   | API     |
| `deckly_rate_limit_rejections_total`                                       | `limit`                   | API     |

Throughput is the rate of `jobs_finished_total`; failure rates are the `failed` share of a
histogram's `_count`; the cache hit rate is `hit` over all lookups. Queue depth is read from
Redis (`ZCARD arq:queue`) on every scrape and is `NaN` when Redis does not answer, rather than a
stale number; every API instance reports the same queue, so aggregate it with `max`. Every label
takes a fixed set of values.

`make observability` starts Prometheus on `:9090` (compose profile `observability`, config in
`observability/prometheus.yml`), scraping `host.docker.internal:8000` and `:9464`. `make up` does not
start it. On Linux, run uvicorn with `--host 0.0.0.0` and set the worker metrics host to `0.0.0.0`
so the container can reach them.

### Traces

OpenTelemetry API and SDK, with spans created by hand rather than by instrumentation packages:

```
POST /v1/generations                        (API, one per request)
└── provider.call topic_moderation
generation.run                              (worker, child of the POST through the queued job)
├── generation.stage.planning             (the cache lookup)
├── generation.stage.retrieving_sources
│   └── provider.call web_search            (one per attempt)
├── generation.stage.parsing_sources
├── generation.stage.generating_cards
│   ├── provider.call card_generation
│   └── provider.call content_moderation
├── generation.stage.fetching_media
│   ├── provider.call image_search          (concurrent)
│   └── provider.call image_moderation
└── generation.stage.finalizing
```

The API puts the W3C `traceparent` of the request into the Arq job as a second argument, and
`run_generation` continues it, so one trace covers the request and the work it queued. Jobs queued
before this existed have no second argument and start a trace of their own. Spans record status,
outcome and error type, never exception messages.

`DECKLY_OBSERVABILITY_TRACE_EXPORTER` is `none` (spans are created, so ids reach the logs, but
nothing is exported) or `console` (one compact JSON line per finished span on stdout, next to the
logs). Exporting over OTLP to Jaeger or similar is a follow-up: it needs
`opentelemetry-exporter-otlp-proto-http`, which there is no deployment target for yet.

## Checks

```bash
make check          # suppression guard, ruff format --check, ruff check (incl. complexity budget),
                    # mypy --strict, import-linter, vulture
make fix            # ruff format + ruff check --fix
make test           # pytest with coverage, no infrastructure needed
make test-integration  # Postgres + Redis tests, appends to the coverage data; needs make up && make migrate
make coverage-gate  # coverage floors; run after make test and make test-integration
make audit          # pip-audit over uv.lock; needs network
make test-live     # calls the model provider configured in .env; needs a real key, costs money
```

CI runs `make install`, `make check`, `make test`, then `make migrate`, `make test-integration` and
`make coverage-gate` against Postgres and Redis service containers (`.github/workflows/ci.yml`,
`backend` job). `make audit` runs in the `backend-audit` job and a gitleaks scan of the full git
history runs in the `secrets` job. The numbers and the reasoning are in
[`.claude/backend/engineering-guide.md`](../.claude/backend/engineering-guide.md) → "CI enforcement".
