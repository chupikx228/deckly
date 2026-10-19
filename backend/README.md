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
                     Gemini), S3-compatible document storage, recording fetches from YouTube
                     (yt-dlp behind an egress proxy), recording transcription (ffmpeg and
                     faster-whisper, each in its own process) and the retry/circuit-breaker layer
  worker/            the two Arq worker entrypoints (generation, transcription), their settings and
                     composition root
stubs/               type stubs for the parts of untyped dependencies the code calls
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

Requires [uv](https://docs.astral.sh/uv/) and Docker, plus ffmpeg and the transcription model for
the transcription worker and `make test-integration` (`brew install ffmpeg`, `make model`; see
"Transcription"). Run everything from `backend/`.

```bash
make install        # uv sync --locked (installs Python 3.12 if needed)
cp .env.example .env
make up             # Postgres on :5433, Redis on :6380, S3Proxy object storage on :9100
make migrate        # alembic upgrade head
make run            # uvicorn on :8000 with reload
make model          # once: the pinned speech model, for the transcription worker
```

Every setting is required; the app refuses to start if one is missing, blank or invalid, and
if Postgres or Redis are unreachable. The database URL must use `postgresql+asyncpg`. The object
store is not checked at startup, so the API boots with it down and only uploads fail.

The worker (`make worker`) consumes the queue that `POST /v1/generations` fills and runs
`application/pipeline.py` (`RunGeneration`): planning → retrieving sources → parsing →
generating cards → fetching media (only when `includeImages` is set) → finalizing. Every stage
change goes through `JobStore.update`, which row-locks the job, so a cancel that lands first
makes the worker stop at its next stage instead of racing it. It runs separately from the API
and shares nothing with it but Postgres and Redis. Jobs from a recording go to a second queue
instead, which `make transcription-worker` consumes (see "Transcription").

Arq's Redis pool has no read timeout of its own, so the API builds its pool itself
(`infrastructure/queue.py`, `create_queue_pool`) with the same connection settings Arq would use
plus a read timeout, and gives every command it sends through the queue that long to finish,
both from `DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS`. The worker builds its pool the same way
(`worker/main.py`) and hands it to Arq's `Worker` as `redis_pool`, instead of letting `Worker`
create its own, which would have no read timeout. A Redis that accepts connections and then
freezes makes the worker's next poll fail after that timeout and the process exit with the Redis
timeout error, as it already does when Redis refuses connections, so run it under a supervisor.
Without the timeout the poll would wait forever, and a cancel's abort marker would never be
delivered. The startup check pings Redis through that same
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
request is counted against the proxy's address. IPv6 peers are counted per /56
(`IPV6_BUCKET_PREFIX_LENGTH` in `infrastructure/quota.py`). A /64 is one host's subnet, but home
ISPs commonly delegate a /56 (RIPE-690's recommendation for residential service) or a /60 to a
household, which hands it 256 or 16 /64s to rotate through, each with its own budget. A /56 puts
the whole delegation in one bucket, the same way one IPv4 address covers a household behind NAT.
It is not a /48 because mobile carriers hand each device a /64 out of a shared pool, so a /48
could cover up to 65,536 unrelated subscribers where a /56 covers at most 256, and IPv4 CGNAT
already lumps users together under the same limit. A household with a /48 delegation can still
use 256 buckets; widen the prefix if that shows up in real traffic, together with
`DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY`, which stays at its IPv4 meaning of one
household. IPv4-mapped IPv6 addresses count as the IPv4 address. A connection with no peer address, which
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
moderation deadlines, plus the worst-case wait for a moderation concurrency slot (see
Concurrency cap), together leave room within `DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS`, and the
same for a job generated from a document (see "Generating from a document").

## Card generation

`infrastructure/card_generator/` implements the `CardGenerator` port with one model call per
job. `DECKLY_PROVIDER_MODEL_PROVIDER` picks the client: `anthropic` (official SDK), `deepseek`
(raw `httpx2` against its OpenAI-compatible `/chat/completions`, JSON output mode) or `gemini`
(raw `httpx2` against `models/{model}:generateContent`, JSON output mode). The same choice
drives every model call: card generation, topic and content moderation, and note regeneration.

For DeepSeek, every request sends `thinking: {"type": "disabled"}`. deepseek-flash thinks at
`high` effort by default, and its reasoning tokens count against `max_tokens`: now and then it
reasons until the cap runs out and answers `finish_reason: "length"` with `content` exactly
empty, which is no JSON to salvage. How often depends on the prompt rather than on chance. On five
real regenerate prompts at the 1000-token cap (October 2026), 7 of 65 replies came back empty,
all from two of the prompts, and they took 6.5–7.6 s, so under the then 4.5 s regenerate timeout they
surfaced as timeouts: 14 of 65 calls ran past 4.5 s. `reasoning_effort: "low"` did not help (7 of
40 empty). With thinking off, none of 65 came back empty, the model took 1.5 s at p50 and 2.7 s
at most, 25 of 25 replies yielded a valid note against 23 of 25 with thinking on, and the
moderation, image-judge and card-generator live tests still pass. DeepSeek's JSON-mode
documentation also warns that the API "may occasionally return empty content"; none came back in
those 65 calls. An empty reply cut at the cap stays an answer, `LlmStop.TRUNCATED` with no text,
for every provider: it is not retried and does not count against the circuit breaker, because it
follows from the prompt and the budget, not from a provider fault, and a retry spends the same
budget on the same prompt.

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

Both content checks from "Content safety" run here (`LlmRegenerationModerator`). The request
check judges the topic together with the rejected card, cut where the model sees it, the same way
the job's topic check judges the topic with the instructions; a block is `422 TOPIC_REJECTED`. It
runs alongside the search, and whichever of the two fails first cancels the other. The note check
hands the one note to the job's `LlmContentModerator` unchanged; a note without an explicit allow
is `503 NO_VALID_CONTENT` and is never returned.

A note of a deck generated from a document cannot be regenerated in v1 (DEC-83). The request names
the deck's `documentId` instead of a `topic`, as in `POST /v1/generations`, and
`RegenerateNoteRequestBody.to_domain` refuses it with `DocumentRegenerationUnsupportedError`,
`422 DOCUMENT_REGENERATION_UNSUPPORTED`, before the use case runs: no budget is spent, nothing is
searched and the document is not read. A job's own download and parse deadlines can exceed this
call's 10 seconds on their own, and a web search for the file name would ground the note in
material different from the rest of the deck, so neither is attempted. The refusal rests on the
client sending `documentId`, because the backend stores no deck; a client that sends its own topic
for such a deck gets an ordinary web-searched note. The design is deferred, as for
`image_occlusion`: see "Resolved decisions", item 6, in `api-contract.md`.

The call must answer within `DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS` (10). Each step has its
own `DECKLY_REGENERATE_*` timeout, separate from the job settings: the rate-limit check in Redis
(0.5 s), the search (4.0 s), the request check (`REQUEST_MODERATION_TIMEOUT_SECONDS`, 4.0 s), the
model (3.25 s) and the note check (`NOTE_MODERATION_TIMEOUT_SECONDS`, 1.75 s). Settings refuse to
load unless the rate limit, the longer of the search and the request check, the model, the note
check and 0.5 s for parsing, validation and the response fit within the total:
0.5 + 4.0 + 3.25 + 1.75 + 0.5 = 10. The request check runs alongside the search, so it gets as
long as the search does at no cost to the total.

The split is not measured for every provider. Measured on deepseek-flash and Tavily (October 2026,
from a development machine in Moscow, 36 regenerations through this wiring and 144 searches with
the regenerate request shape, spread over two and a half hours of one afternoon):

| Step                                  | p50    | p90    | max    | Over its timeout        |
| ------------------------------------- | ------ | ------ | ------ | ----------------------- |
| Search, query Tavily has not cached   | 2.0 s  | 2.9 s  | 4.3 s  | 8 % at 3.0 s            |
| Search, same, on a new connection     | 2.4 s  | 2.9 s  | 4.3 s  | 10 % at 3.0 s           |
| Search, query repeated within minutes | 0.5 s  | 1.1 s  | 4.8 s  | (cached by Tavily)      |
| Request check                         | 1.0 s  | 1.5 s  | 1.7 s  | none                    |
| Model, useful replies, thinking on    | 3.0 s  | 4.2 s  | 4.5 s  | 25 % timed out at 4.5 s |
| Note check                            | 1.0 s  | 1.3 s  | 1.4 s  | none                    |
| Parsing, validation and the response  | 0.07 s | 0.07 s | 0.37 s | none                    |

A regenerate searches the deck's topic, usually long after the job searched it, so the uncached
rows are the ones that count, and on a quiet API process the connection is usually new as well
(the client keeps idle connections for 5 s). At the earlier 2.5 s search timeout 27 % of uncached
searches timed out, 40 % on a new connection, and per-round medians drifted between 1.5 s and 2.9 s
across the afternoon. Connecting to Tavily takes about 0.35 s, and leaving out the raw page content
saves about 0.3 s, so neither explains the latency. The search went from 2.5 s to 3.0 s, taking
the 0.5 s from the overhead, which used at most 0.37 s. The model and the note check then
reached close to their timeouts, and the rate limit was not measured until later (see below), so
none of them gave time.
A wider search would cut timeouts only from 8 % to 3 % at 3.5 s, small next to the model's 25 %:
about one call in four ran into the 1000-token output cap with no content at all, which no
timeout rescues. Earlier measurements put the search at 0.2–1.6 s because the live test then
searched the same topic every run and Tavily answered from its cache.

That 25 % was DeepSeek reasoning until the output cap ran out (see Card generation). With
thinking off, measured the same way (October 2026, 36 regenerations over 20 minutes):

| Step          | p50    | p90   | max   | Over its timeout |
| ------------- | ------ | ----- | ----- | ---------------- |
| Search        | 1.8 s  | 2.4 s | 3.0 s | 1 of 36 at 3.0 s |
| Request check | 1.0 s  | 1.3 s | 1.4 s | none             |
| Model         | 1.35 s | 1.7 s | 2.4 s | none             |
| Note check    | 0.9 s  | 1.2 s | 1.4 s | none             |
| Whole request | 4.0 s  | 5.0 s | 6.1 s | none at 10 s     |

35 of 36 regenerations succeeded; the one failure was the search timeout. The model then left
about 2 s of its 4.5 s unused, so the search, the model and the note check were measured again
over a longer stretch, the same way as the first table: 12 rounds over two hours and ten minutes
of one evening (October 2026), 144 searches with the regenerate request shape and 48
regenerations through this wiring, every step given 15 s so that slow calls are timed to the end
rather than cut off at their timeouts:

| Step                                 | p50    | p90    | max    | Over 3.0 s | Over 3.5 s | Over 4.0 s | Over 4.5 s |
| ------------------------------------ | ------ | ------ | ------ | ---------- | ---------- | ---------- | ---------- |
| Search, query Tavily has not cached  | 1.95 s | 3.2 s  | 8.6 s  | 13 %       | 8 %        | 4 %        | 2 %        |
| Search, same, on a new connection    | 2.2 s  | 3.6 s  | 8.6 s  | 17 %       | 11 %       | 6 %        | 4 %        |
| Request check                        | 0.9 s  | 1.2 s  | 1.45 s | none       |            |            |            |
| Model                                | 1.35 s | 1.75 s | 2.0 s  | none       |            |            |            |
| Note check                           | 0.8 s  | 1.1 s  | 1.15 s | none       |            |            |            |
| Parsing, validation and the response | 0.05 s | 0.06 s | 0.07 s | none       |            |            |            |

The search rows count 192 uncached searches: the 144 above and the 48 the regenerations ran.
Per-round search medians drifted between 1.5 s and 3.5 s across the evening, against 1.5–2.9 s
across the earlier afternoon, and the model's per-round medians stayed between 0.9 s and 1.7 s.
The live test regenerates only basic notes in English, so 18 more regenerations covered multiple
choice, cloze and basic notes in English and Russian, each on new connections: the model took at
most 2.0 s there too, the note check at most 1.3 s.

The search was the only step that timed out, so it got the time: from 3.0 s to 4.0 s, taken from
the model, which went from 4.5 s to 3.5 s. That cuts search timeouts from 13 % to 4 %, and from
17 % to 6 % on a new connection, and leaves the model 1.75× the slowest reply measured here and
0.8 s over the slowest measured with thinking off at all (2.7 s). Going further, to 4.5 s and
3.0 s, would save two more searches in a hundred, and the model would then have only 0.3 s over
that slowest reply. A model timeout is also the costlier failure, because it comes after the search
has already spent its time. The rest of the search tail does not shrink with any timeout that
fits: one search took 8.6 s and another 11.9 s. The note check keeps its 1.5 s: it never came
within 0.2 s of it. The request check keeps its 2.5 s, which it never came within a second of; it
runs alongside the search, so it does not lengthen the request.

The rate limit had carried its 0.5 s over from the first split without ever being measured, and
both search campaigns above ran on a Sunday, so both were measured next (October 2026).
`RedisRegenerationLimiter.acquire`, wired the way the API wires it (the shared queue pool, pinged
at startup), was timed against the local Docker Redis with a 15 s command timeout:

| Rate-limit check                                | n    | p50     | p99     | max     |
| ----------------------------------------------- | ---- | ------- | ------- | ------- |
| Back to back, a new client each time            | 500  | 0.32 ms | 0.45 ms | 2.2 ms  |
| A client over its limit (rejected)              | 70   | 0.32 ms | 0.44 ms | 0.45 ms |
| First check on a new connection                 | 50   | 1.1 ms  | 13.7 ms | 13.7 ms |
| 100 at once, once the pool has grown            | 1900 | 7.0 ms  | 15.8 ms | 18.3 ms |
| In the regenerate flow, over the campaign below | 192  | 2.7 ms  | 6.2 ms  | 6.7 ms  |

The check used at most 18 ms of its 500, and only with a hundred running at once. It keeps the
0.5 s anyway: this is a Redis on the same machine, there is no production deployment yet, and a
check that outlasts its timeout fails the request closed with 503 rather than letting it through
unmetered. A Redis across a network needs a round trip per command, and a few more with TLS on a
new connection, where this one needed none. Giving 0.3 s of it to the search would cut search
timeouts only from 3 % to 1.6 %. Measure it on the deployed Redis before taking time from it.

The weekday was measured the same way as the evening above: 12 rounds over two hours and fifteen
minutes of a Monday, 19:55–22:10 Moscow time, 144 searches with the regenerate request shape and 48
regenerations through this wiring, every step given 15 s. Tavily answers from AWS us-east-1, so
those hours are 12:55–15:10 US Eastern, the middle of a US working day; Moscow's midday is the US
night, Tavily's quietest hours, so it was not measured separately. Each search had a topic of its
own (road signs by country, kind of sign and setting, 960 combinations), so none was cached, and
132 of the 192 ran on a new connection.

| Uncached search            | Sunday afternoon | Sunday evening | Monday, US midday |
| -------------------------- | ---------------- | -------------- | ----------------- |
| p50                        | 2.0 s            | 1.95 s         | 2.2 s             |
| p90                        | 2.9 s            | 3.2 s          | 3.4 s             |
| max                        | 4.3 s            | 8.6 s          | 5.4 s             |
| Over 3.0 s                 | 8 %              | 13 %           | 20 %              |
| Over 4.0 s                 |                  | 4 %            | 3 %               |
| Over 4.5 s                 |                  | 2 %            | 1.6 %             |
| New connection, over 4.0 s |                  | 6 %            | 4 %               |

Sunday afternoon is the first table in this section, Sunday evening the third. The weekday moves
the body of the distribution, not the tail that times out: a fifth of the searches took over 3.0 s,
against an eighth on Sunday evening, but no more of them went over 4.0 s, and the slowest took
5.4 s, against 8.6 s and 11.9 s on Sunday. Per-round medians drifted between 1.8 s and 3.2 s, the
first round the slowest. The search keeps its 4.0 s.

The other steps, in those 48 regenerations, all of which succeeded:

| Step                                 | p50    | p90    | max    |
| ------------------------------------ | ------ | ------ | ------ |
| Request check                        | 1.0 s  | 1.55 s | 2.3 s  |
| Model                                | 1.4 s  | 1.9 s  | 2.7 s  |
| Note check                           | 0.8 s  | 1.1 s  | 1.57 s |
| Parsing, validation and the response | 0.05 s | 0.07 s | 0.08 s |
| Whole request                        | 4.8 s  | 6.15 s | 7.4 s  |

The note check went over its then 1.5 s once, after the search and the model had already spent
their time, which makes it the costliest step to time out. The request check came within 0.25 s of
its then 2.5 s, against 1.45 s at most on Sunday evening. So the note check got 0.25 s from the
model: 1.5 s to 1.75 s, and 3.5 s to 3.25 s, which still leaves the model 0.5 s over the slowest
reply measured with thinking off (2.7 s, here and before). The request check went from 2.5 s to
4.0 s, as long as the search it runs alongside; the budget counts only the longer of the two, so
that costs the total nothing.

`tests/live/test_regeneration_live.py` (`make test-live`) times every step against the configured
provider and fails when one does not fit its timeout: run it after switching provider or model.
Each sample searches its own topic, drawn at random from 160 road-sign topics (20 countries × 8
kinds of sign), so the search leg measures a query Tavily has not cached. Two back-to-back runs
repeat a topic about one time in twenty. The topics stay on road signs so the rejected card the
test sends still belongs to the deck. With 3 % of searches over the 4.0 s timeout, as measured on
Monday, a run of three regenerations fails on a slow search about one time in eleven, so read a
failure from the `regeneration_latency_measured` line it logs, not as a regression on its own.

Search, model and both checks get **one attempt each, no retry**: a second attempt after a timeout
cannot fit in what is left, and one that follows an instant `429`/`5xx` is better left to the
user tapping again, told when by `retryAfterSeconds`. Timeouts, transient errors and an open
circuit are `503 UPSTREAM_UNAVAILABLE`. Each step already has its own timeout; on top of that, the
whole use case runs under `asyncio.timeout` of the total, which cancels whatever is still running
and answers 503. Search, model and each check go through their own circuit breakers in the API
process, configured with the job's `DECKLY_PROVIDER_*_CIRCUIT_*` values (the checks use the
model's); breaker state is per process, so it is not shared with the worker's. The model's output is capped at
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

The port also reports why each searched note has no image: `rejected` (no search hit, no
candidate with a derivable licence, or none the classifier accepted, which a retry would most
likely repeat) or `failed` (the search errored, its circuit was open, or it was cut off by the
stage deadline, which a retry may well fix). A job whose result has any `failed` note is
returned but not cached, exactly like one whose media stage raised; `rejected` notes do not
count, and a request without `includeImages` never has any. The pipeline logs the count as
`failed_image_notes` on `media_attached`.
The port returns attachments keyed by `clientId`, never notes, so a media adapter cannot drop or
duplicate a note; attachments for unknown notes are ignored and a repeated `mediaId` is kept once.

## Content safety

`infrastructure/moderation/` screens topics and generated content against one content policy
(`moderation/prompt.py`), using the configured model provider with a separate, cheap model
(`DECKLY_PROVIDER_MODERATION_MODEL_NAME`, `claude-haiku-4-5` in `.env.example`). Untrusted text
reaches the classifier as a JSON document, so it cannot break out of its delimiters.

- **Topic check** (`LlmTopicModerator`, wired in `main.py`): runs in `POST /generations` after the
  quota is reserved and before the job is stored. The topic and the `instructions` are judged
  together; a request from a document is judged by its file name and `instructions`. A block is `422 TOPIC_REJECTED` and spends the quota unit. There is one attempt,
  bounded by `DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS` (default 2 s, at most 10). A timeout,
  provider error or unreadable verdict is `503 UPSTREAM_UNAVAILABLE` and gives the unit back.
  Published latency for Haiku 4.5 is roughly 0.6–0.8 s to the first token, so the request
  normally takes about 1 s rather than the 500 ms it took before this check.
- **Generated-content filter** (`LlmContentModerator`, wired in the worker): runs after
  `generating_cards`, before media. Notes go to the classifier in chunks of
  `NOTES_PER_CONTENT_CHECK` (50), each chunk with the deck and its notes numbered from 1, and
  every chunk of a job runs at once: at most four calls at the 200-card maximum, one call at 50
  cards or fewer. A note is kept only on an explicit `"allow"`; a missing or unreadable verdict drops it.
  The classifier sees each note's type, fields and tags, never its sources, so notes written from
  an uploaded document are screened exactly like notes written from web pages.
  A deck title that is not allowed by every chunk is replaced with the topic (the file name for a
  document). A refused chunk drops
  its own notes and the deck title. If any chunk fails, the whole filter fails like a single call
  would (`provider_unavailable`), and the other chunks are cancelled, so no partial deck ships.
  Retries follow `DECKLY_PROVIDER_MODERATION_FILTER_{TIMEOUT_SECONDS,DEADLINE_SECONDS,
MAX_ATTEMPTS}` per chunk. Because the chunks run concurrently, the filter's wall time stays within
  one deadline, which is what counts towards the generation job timeout. Results are cached after
  filtering, and a cache hit is not screened again.

  Chunk size, measured on deepseek-flash (October 2026), with the 15 s attempt timeout, 20 s
  deadline and 4000-token output cap: one 200-note call took 4.3–5.4 s and about 1,300 output
  tokens, reasoning included, whether the notes were short (17k input tokens) or three times
  longer than the generator's 16k-token output cap allows (54k). Latency follows the reply, not
  the input. 50-note chunks took 1.7–3.4 s and 330–610 output tokens. Ten 200-card runs of four
  concurrent chunks took 2.1–3.0 s wall time. The single call already fit its timeout, so chunking
  buys two things: a retry that fits (a second attempt only fits when the first fails within about
  5 s, minus backoff), and replies about 6× under the output cap instead of 3×, which mattered
  because of DeepSeek's empty replies at the cap. Those figures were measured with DeepSeek's
  thinking on; with it off (see Card generation), the 200-card live test of four concurrent
  chunks took 2.4 s.

- **Concurrency cap** (`BoundedLlmClient`, wired in `worker/settings.py`): Arq runs up to ten
  jobs per worker process (`WORKER_MAX_JOBS`, pinned as `max_jobs` in `WorkerSettings`), so ten
  maximum-size decks would otherwise put forty chunk calls in flight at once against one
  provider key. One semaphore per worker process, sized by
  `DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY` (default `WORKER_MAX_JOBS`, 10, at most 40),
  bounds every call of the content filter and the image judge together, whatever job it belongs
  to. Because the default is `WORKER_MAX_JOBS` itself, raising the job count raises it too. A
  worker at full load with decks of up to 50 cards never queues, and one 200-card job still
  runs its four chunks together. Only bursts of larger decks
  queue: ten 200-card jobs take four waves of chunk calls instead of one, which at the measured
  2–3 s per chunk adds roughly 10 s to the slowest job.

  Startup refuses a configuration where that wait could exhaust the job timeout. A call queues
  behind at most `WORKER_MAX_JOBS` × chunks per maximum deck − 1 others (39), freed
  `DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY` at a time, so it waits for at most
  ⌊39 / concurrency⌋ waves (3 at the default of 10, none at 40). A wave lasts as long as the
  longest a slot can be held, which is the larger of the filter and the image judge deadlines
  (20 s), not the 15 s attempt timeout: a slot is held across retries, and a retry only starts
  when it can finish inside the deadline. A job passes the gate twice, at the content filter and
  at the image judge, so the bound is 2 × 3 × 20 = 120 s, added to the 335 s of stage deadlines;
  a job from a document passes it a third time, at the document screen (3 × 3 × 20 = 180 s).
  That is the worst case on purpose: the stage deadlines in the same sum are bounds, not typical
  durations, and a bound built from the typical 2–3 s per chunk would not follow the configured
  deadlines and would pass exactly when a slow provider makes the wait matter. It is slightly
  conservative, since the judge call queues behind at most nine other jobs' chunks, not ten.
  `DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS` is 570 in `.env.example`: the 555 s a document
  job can need (see "Generating from a document") and 15 s of margin.
  The cap is not a breaker or a retry: excess calls wait for a slot and are never rejected. It
  sits outside the resilience layer, so waiting for a slot is not charged to an attempt timeout,
  the retry deadline or the circuit; it does count toward the generation job timeout. A slot is
  held across the retries of a call, which is deliberate while a provider is rate limiting. The
  API process is not capped: the topic check and the regeneration checks are one call per
  request, sequential within it, and are interactive paths with single-attempt timeouts of
  seconds, where queueing would add latency instead of smoothing a burst.

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
filter. `POST /v1/notes/regenerate` runs the topic and content checks too; see "Note regeneration".

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

| Event                                                                                                                                       | Where                  | Carries                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- | -------------------------------------------------------------------------- |
| `http_request_finished`                                                                                                                     | API, every request     | method, route template, status, `duration_ms`                              |
| `generation_admitted`                                                                                                                       | API                    | `admission` (`queued` or `replayed`), status, `source_kind`, `document_id` |
| `topic_rejected`, `rate_limit_rejected`                                                                                                     | API                    | `limit` for the rate limit                                                 |
| `generation_started`                                                                                                                        | worker                 | status, stage                                                              |
| `generation_cache_checked`                                                                                                                  | worker                 | `result` (`hit`, `miss`, `corrupt`, `error`)                               |
| `generation_stage_entered`                                                                                                                  | worker, every stage    | stage, progress                                                            |
| `provider_call_finished`                                                                                                                    | every provider attempt | `operation`, `attempt`, `outcome`, `duration_ms`                           |
| `provider_call_retrying`, `provider_call_refused`, `circuit_opened`, `circuit_closed`                                                       | resilience layer       | `operation`                                                                |
| `sources_retrieved`, `sources_parsed`, `card_generation_finished`, `content_screened`, `images_screened`, `media_fetched`, `media_attached` | adapters               | counts only                                                                |
| `generation_succeeded` / `generation_failed` / `generation_stopped`                                                                         | worker                 | `duration_ms`, failure code, traceback on failure                          |
| `generation_cancelled`                                                                                                                      | API                    | stage and progress at the time                                             |

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

| Metric                                                                     | Labels                              | Process |
| -------------------------------------------------------------------------- | ----------------------------------- | ------- |
| `deckly_generation_jobs_admitted_total`                                    | `outcome`, `source`                 | API     |
| `deckly_generation_jobs_cancelled_total`                                   |                                     | API     |
| `deckly_generation_jobs_finished_total`                                    | `outcome`, `failure_code`, `source` | worker  |
| `deckly_generation_job_duration_seconds`                                   | `outcome`, `source`                 | worker  |
| `deckly_generation_stage_duration_seconds`                                 | `stage`, `outcome`, `source`        | worker  |
| `deckly_provider_call_duration_seconds`                                    | `operation`, `outcome`              | both    |
| `deckly_provider_call_retries_total`                                       | `operation`                         | both    |
| `deckly_provider_circuit_rejections_total`, `deckly_provider_circuit_open` | `operation`                         | both    |
| `deckly_generation_cache_lookups_total`                                    | `result`                            | worker  |
| `deckly_transcript_cache_lookups_total`                                    | `result`                            | worker  |
| `deckly_generation_queue_depth`                                            | `queue`                             | API     |
| `deckly_gate_in_flight`                                                    | `gate`                              | worker  |
| `deckly_gate_limit`                                                        | `gate`                              | worker  |
| `deckly_gate_wait_seconds`                                                 | `gate`, `outcome`                   | worker  |
| `deckly_rate_limit_rejections_total`                                       | `limit`                             | API     |

Throughput is the rate of `jobs_finished_total`; failure rates are the `failed` share of a
histogram's `_count`; the cache hit rate is `hit` over all lookups. Queue depth is read from
Redis (`ZCARD arq:queue`) on every scrape and is `NaN` when Redis does not answer, rather than a
stale number; every API instance reports the same queue, so aggregate it with `max`. Every label
takes a fixed set of values. `source` is `topic` or `document`, and empty on a job that ended before
its request was read (skipped or unreadable); sum it away for totals.

`deckly_gate_in_flight{gate="moderation"}` is how many calls hold a slot of the worker's moderation
concurrency gate right now (content and image moderation share it), and
`deckly_gate_limit{gate="moderation"}` is its ceiling, `DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY`,
so saturation is `deckly_gate_in_flight / deckly_gate_limit`.

`deckly_gate_wait_seconds{gate="moderation",outcome}` is how long a call queued for a slot, 0 when
one was free. `outcome` is `acquired` when the call got its slot, and `abandoned` when it was
cancelled while still queued (its job timed out or was stopped): that observation is the time it
had waited up to the cancellation. The worst waits are therefore in `abandoned`; watch
`rate(..._count{outcome="abandoned"})` against the `acquired` rate.

The API process has a gate of its own, `gate="document_upload"` (see "Documents"), reported with the
same three series, so the same saturation query works for it. It never queues, so every wait it
records is 0 in `acquired` and `abandoned` never appears; the calls it turned away are counted by
`deckly_gate_rejections_total{gate="document_upload"}`. The two processes expose different `gate`
values: the worker never has `document_upload`, the API never has `moderation`. Each API process has
its own gate, so aggregate `deckly_gate_in_flight` with `sum` across instances and read the limit of
one.

`make observability` starts Prometheus on `:9090` (compose profile `observability`, config in
`observability/prometheus.yml`), scraping `host.docker.internal:8000` and `:9464`. `make up` does not
start it. On Linux, run uvicorn with `--host 0.0.0.0` and set the worker metrics host to `0.0.0.0`
so the container can reach them. Neither port is meant to be public: see "Production deployment"
below.

### Traces

OpenTelemetry API and SDK, with spans created by hand rather than by instrumentation packages:

```
POST /v1/generations                        (API, one per request)
└── provider.call topic_moderation
POST /v1/notes/regenerate                   (API, one per request)
├── provider.call regeneration_search       (concurrent with the request check)
├── provider.call regeneration_request_moderation
├── provider.call note_regeneration
└── provider.call regeneration_note_moderation
generation.run                              (worker, child of the POST through the queued job)
├── generation.stage.planning             (the cache lookup)
├── generation.stage.retrieving_sources
│   └── provider.call web_search            (one per attempt; document_download for a document)
├── generation.stage.parsing_sources
│   └── provider.call document_moderation   (a document only)
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

## Documents

`POST /v1/documents` stores a PDF or `.pptx` the client uploads, and `GET /v1/documents/{id}`
returns its metadata to the same client. The contract is in `api-contract.md`, the write order
and the retention sweep in `engineering-guide.md` ("Document uploads", "Scheduled housekeeping").
`application/documents.py` runs the upload; the adapters are in `infrastructure/documents/`
(content sniffing, the `documents` table) and `infrastructure/storage/` (the object store). Audio
and video recordings are uploaded through `POST /v1/recordings` into the same table, store and
retention, with their own gate and probe (see "Recordings" below).

**Type detection** (`documents/sniffing.py`) reads the bytes, never the name or the declared
`Content-Type`, and runs in a thread. A PDF needs a `%PDF-1.x`/`%PDF-2.x` header starting in its
first 1024 bytes (Acrobat's tolerance for leading junk) and a `%%EOF` in its last 1024, which
catches truncated uploads. A presentation needs the zip signature at offset 0, a central directory
`zipfile` can read, `[Content_Types].xml` and `ppt/presentation.xml` entries, no
`ppt/vbaProject.bin`, and the presentation main content type in `[Content_Types].xml`. That part is
read with a 1 MiB cap and searched as bytes, not parsed as XML, and nothing else in the archive is
decompressed. A damaged archive (bad central directory, a corrupt deflate stream, a bad checksum, an
encrypted entry) is a `415`, not a `500`. This is a gate, not a parser: a file that passes can still
fail text extraction, and zip-bomb limits on the full archive belong to the parser (DEC-75).

**Size** (`DECKLY_LIMIT_DOCUMENT_MAX_BYTES`, 25 MiB, at most 100 MiB). The upload is held in memory
for hashing and the PUT, so the limit is also the memory one upload can take. 25 MiB comfortably
fits text-heavy lecture PDFs and slide decks; generation only uses the extracted text, and a deck
past 25 MiB is almost always images that would be thrown away. On a 5 Mbit/s mobile uplink it takes
about 40 s to send. The transport enforces it twice: a `Content-Length` over the limit plus 16 KiB
of multipart overhead is refused before the rate limit or the body is touched, and the request body
is read through a counter that raises as soon as it passes that bound, which also covers chunked
uploads without a length. Starlette's multipart parser caps form fields but not file parts, so the
counter is what keeps an oversized upload off the disk.

**Rate limit** (`infrastructure/upload_limit.py`). Fixed windows like regeneration, with its own
keys (`deckly:document-upload:client:*`, `deckly:document-upload:address:*`): 10 uploads per client
and 50 per address per hour by default. Uploads exist to feed generations, which are capped at 20 a
day, so 10 an hour leaves room for retries and wrong files while capping one client at 250 MB of
upload bandwidth an hour (480 MiB if every upload is a full-size recording, which shares this
budget). The address backstop shares the generation quota's bucketing (IPv6 /56).
Both counters are incremented in one `MULTI`; a Redis that does not answer within
`DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS` fails the upload with `503`, like the generation quota.

**Concurrency cap** (`infrastructure/upload_gate.py`,
`DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY`, default 4 since DEC-84, 8 before, 1 to 32). The rate
limits bound one client or address; nothing bounded the API process, and an upload is held in
memory, so many clients uploading at once could exhaust it. The gate is an `asyncio.Semaphore` built
once when the API starts, one per process, and an upload holds a slot from before the rate limit is
checked until the object is stored (or the request fails or is cancelled).

_Why 8, originally (DEC-78)._ There is no deployment to measure, so this is an assumption, stated so it can be revisited.
Reading a 25 MiB upload through `read_upload` holds 25.8 MiB of Python allocations at its peak
(`tracemalloc`, measured in this repository, the multipart parser spools to disk past 1 MiB), and the
PUT hashes and sends those same bytes without copying them. Allowing about twice that for allocator
overhead and the HTTP client's buffers gives 50 MiB per upload, which is the figure DEC-78 started from.
The assumed budget is a 1 GiB API container with about 200 MiB of baseline (FastAPI,
SQLAlchemy, the HTTP clients) and half of what remains kept free for the other endpoints and for
garbage collection: roughly 400 MiB, and 400 / 50 = 8. A slot is sized for a full 25 MiB body, but an
upload only holds what it has received so far, so a typical few-MiB lecture deck costs far less and 8
slots is conservative for the usual mix. **The cap and `DECKLY_LIMIT_DOCUMENT_MAX_BYTES` are tied, and
the tie is enforced at startup:** the process refuses to start when cap x size limit x 2 exceeds
`DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES` (400 MiB, the budget above, until DEC-85 raised it to
1,288 MiB, see below), and the error
names the three values and the settings to change. Raising the size limit to 100 MiB with a cap of 8
puts the worst case at 1.6 GiB, so the budget, the cap or the limit has to move with it.

_Since DEC-84 the budget is shared with recording uploads_, which have their own gate (2 slots of 48
MiB) in the same process, so the check is the same formula summed over both gates: (document cap x
document limit + recording cap x recording limit) x 2 <= budget. Keeping the 1 GiB container and the
400 MiB budget, the document cap went from 8 to 4: (4 x 25 + 2 x 48) x 2 = 392 MiB, 8 MiB under the
budget.

_Since DEC-85 the check also carries the URL fetch's subprocesses._ The two recording slots became one
for uploads and one for URL fetches (`DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY`). A URL slot holds
up to a recording's worth of bytes in the API process, like an upload, and while it runs, yt-dlp and
deno run beside the API in the same container. Their summed resident memory peaked at **394–422 MiB**
during the metadata step (three runs that needed deno's JavaScript solver; 59 MiB in one that did not)
and 48–55 MiB during the download, measured on macOS by polling the RSS of the whole process group
every 10 ms for a 19-second and a 3.5-minute video. `RECORDING_URL_SUBPROCESS_PEAK_BYTES` is that peak
rounded up to 448 MiB, per URL slot, and it goes **inside the same x 2 margin** as every other term:
the figure comes from four samples on macOS, the deployment is likely Linux with a different Deno/V8
memory profile, and the check treats every term the same way rather than special-casing one. So the
check is now (document cap x document limit + (recording cap + URL cap) x recording limit + URL cap x
448 MiB) x 2 <= budget, and the defaults need (4 x 25 + 1 x 48 + 1 x 48 + 1 x 448) x 2 = **1,288 MiB**,
which is the new default budget, with nothing to spare (the old 400 MiB cannot hold even one URL
slot: 448 x 2 alone is 896 MiB). **The container assumption moves from 1 GiB to about 2 GiB:** 200 MiB
of baseline plus the 1,288 MiB budget leaves about 560 MiB for the other endpoints and garbage
collection, less than the half DEC-78 kept free (that posture would need about 2.75 GiB); the slack now
lives in the margin inside the budget. Measure the subprocess peak on the deployment target and adjust
the constant if Linux differs. Four concurrent document uploads is still far above what per-client limits produce with no
production traffic; revisit with `deckly_gate_rejections_total{gate="document_upload"}` if 4 proves
tight, by raising the budget to what the container really has rather than by trimming the margin.
The 2 is the margin measured above and is not a setting. The budget is an assumption about the
container, not something the process reads from it: set it to what the container can actually spare.
**The check assumes a single process.** The limit and the budget are per process, so with several
workers or replicas sharing one container's memory, divide the budget by their number (or lower the
cap) yourself; the check cannot see them.

The worker holds documents too, and has its own budget and check (see _Worker memory_ under
_Generating from a document_).

_Reject, not queue._ An upload is a user waiting at a screen, and a slot is held for as long as the
client takes to send the body, up to about 40 s on a 5 Mbit/s uplink, plus the object-store PUT (60 s
deadline). Queueing behind a full gate would add up to that much to a request that already takes that
long, past a mobile client's timeout and a reverse proxy's body-read and response timeouts, and a
request the client has given up on would still be queued and would take a slot when it came up
(`ClientDisconnect`). A full gate therefore answers `503 UPSTREAM_UNAVAILABLE` at once with
`Retry-After: 5`, the hint every other transient `503` in the API uses (`STORAGE_FAULT`, provider
faults): a typical upload finishes in a few seconds, so the first retry usually finds a slot. This
deliberately differs from the moderation gate, which queues because its callers are background
worker chunks with a job-length deadline. No new error code is needed (`UploadCapacityExceededError`
is an `UpstreamUnavailableError`, so the client already retries it), and the retries are not
jittered: with 4 slots and the per-client limits the herd is small.

_Read deadline_ (`DECKLY_LIMIT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS`, default 120, 1 to 900). A slot is
held while the body is received, and the cap means that exactly 4 connections sending a few bytes a
minute would hold every slot and refuse every other upload. `read_upload` therefore runs under one
`asyncio.timeout` around the whole receive loop, the same bounded-I/O pattern as the rest of the
service. It is a deadline for the _whole body_, not an idle timeout: a trickling client sends
something just before any idle timeout fires, so only a total deadline bounds the slot. On expiry the
upload fails with the same quiet `400` as a disconnect (`InvalidDocumentError`), the gate releases
the slot as the exception unwinds, and `upload_read_timed_out` is logged at warning level.

_Why 120 s._ A full 25 MiB body takes about 40 s at 5 Mbit/s, so 120 s is three times that, and it
still admits a full-size upload at about 1.75 Mbit/s sustained (a poor but usable mobile link); a
typical few-MiB deck at 1 Mbit/s takes well under a minute. A client slower than that is better
served by a quick failure than by a connection that outlives the app's own request timeout. Longer
would let an attacker hold a slot longer per connection; with the cap at 4, each slow connection
costs at most 120 s of one slot, and each costs one unit of the upload budget (the limiter runs
before the body is read), so one client or address cannot do this for long: 10 uploads per client
per hour and 50 per address. Raise the value together with `DECKLY_LIMIT_DOCUMENT_MAX_BYTES`, since the
time a legitimate upload needs grows with the size limit. A `400` was chosen over `408` because the
contract has no error code for it and a client that is told its upload is invalid does not retry
blindly; a distinct `408 UPLOAD_TIMEOUT` is the natural next step if the app needs to tell the two apart.

The gate is taken before the rate limit, so a refused upload never reaches the Redis counters and
retrying it does not spend the client's budget; it is refused before the body is read, so the refusal
costs no memory. A `Content-Length` over the size limit is still refused first, without taking a slot.
The slot is held only by the uploads themselves, so `GET /documents/{id}` is not affected.

**Retention.** 24 hours from upload (`DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS`), the same window as
job retention and idempotency keys: long enough for a user who uploaded, backgrounded the app and
came back the next day, and no longer, because a document is the user's own content and the backend
is not its system of record. A document a generation used is kept until job retention purges the
last job that used it (see "Generating from a document").

### Parsing

`infrastructure/documents/parser.py` (`DocumentTextParser`) implements the `DocumentParser` port:
it turns a stored document's bytes into `SourceMaterial`, one entry per page or slide that has
text, each cited by a `DocumentSource` (`documentId`, `unit` `page` or `slide`, 1-based `number`,
the file name as `title`). The entries have exactly the shape web search produces, so the card
generator and the content filter need no changes, and they go through the same cleaning
(`search/cleaning.py`): control and invisible characters are stripped, `<source>`-shaped markers in
the text and in the file name are defused, a unit more than a tenth binary garbage is skipped (a
font without a Unicode map extracts as garbage). Units without text are skipped without shifting
the numbering. Text is cut at a word boundary once `max_characters` is used, and later units are
never read; a document with fewer than 50 visible characters in total fails with
`DOCUMENT_HAS_NO_TEXT`. Extraction runs in a thread, bounded by a wall-clock deadline checked
between units and, for PDFs, before every content-stream operator. The worker wires it with
`DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS` and `DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS`.

`ResilientDocumentStorage.get` (`GET` on the object store, its own `document_download` breaker and policy,
`DECKLY_PROVIDER_STORAGE_DOWNLOAD_*`) streams the object with a cap of the size recorded at upload,
so an object that grew in the store is refused rather than read. An object that is gone is
`DocumentNotFoundError` and does not count toward the breaker.

**PDF: `pypdf`** (new dependency, 6.20.1, BSD-3, pure Python, no dependencies of its own, typed,
released every one to three weeks). Compared with:

- **pdfplumber**: better layout and table extraction from character positions, which flashcard
  input does not need, at the cost of four dependencies (pdfminer.six pinned to one release,
  Pillow, pypdfium2, cryptography), three of them native, and pdfminer's speed.
- **pypdfium2** (Chromium's PDFium): the fastest and the best text order, but a crash in its C++
  takes the worker process and every job in it down with it, and its memory cannot be bounded from
  Python.
- **PyMuPDF**: AGPL, ruled out.

pypdf follows content-stream order, which for most producers is column by column; tables come out
row by row without structure. A malformed file raises instead of crashing the process, and pypdf
ships its own limits against hostile files, which are tightened here (`documents/pdf.py`):

| Limit                                                                     | Value  | Why                                                                                                                                                                      |
| ------------------------------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Decoded size of one stream (Flate, LZW, RunLength, JBIG2, Brotli, arrays) | 16 MiB | Real page content is under 1 MB; pypdf's default is 75 MB                                                                                                                |
| Decoded page content, all pages together                                  | 64 MiB | pypdf keeps decoded streams; 500 pages of 16 MiB would be 8 GB                                                                                                           |
| Pages in the page tree                                                    | 5,000  | pypdf flattens the whole tree before the first page: 99,000 empty pages in 14 MiB took 23 s and 326 MiB before this cap, 4 s and 47 MiB after (both under `tracemalloc`) |
| Form XObject invocations per page                                         | 500    | pypdf re-parses a form at every `Do`                                                                                                                                     |
| Pages read                                                                | 500    | Past the character budget of any job                                                                                                                                     |

A tiny PDF that replays one heavy form on one page ran for 37 s in testing even with these limits,
so the deadline is also checked from pypdf's per-operator callback: the run stops within one
form's work of the deadline instead of at the end of the page. pypdf's own warnings, one per
damaged object, are not logged (an adversarial file could flood the log); the `document_parsed`
line records the outcome per unit.

Encryption: a PDF whose user password is empty (it only restricts editing or printing) is opened
and read. One that needs a password is `DOCUMENT_ENCRYPTED`. AES decryption needs `cryptography`,
which is not a dependency, so an AES-encrypted PDF is `DOCUMENT_ENCRYPTED` even when it would open
without a password; adding it is the fix if failure metrics show users hitting this. Upload-time
sniffing does not detect encrypted PDFs, so this is where they are caught.

**PPTX: the standard library, not python-pptx.** python-pptx 1.0.2 (August 2024) reads every part
of the archive into memory when it opens a file (`pptx/opc/serialized.py`: `{name: z.read(name)
for name in z.namelist()}`), so a zip bomb exhausts memory before any check runs, and a normal
deck has all its images inflated for nothing. It would also add Pillow, XlsxWriter and lxml.
`documents/pptx.py` reads only the parts it needs, with `zipfile` and a streaming `pyexpat`
parser: slide order from `p:sldIdLst` in `ppt/presentation.xml` through its relationships, text
from `a:t` with a line per `a:p` and `a:br`, speaker notes from the notes body placeholder
attributed to their slide. Slide numbers count hidden slides, as PowerPoint does. Slide number,
date, footer and header placeholders, field text (`a:fld`) and `mc:Fallback` copies are left out;
tables are kept. Transitional and Strict OOXML namespaces are both read. SmartArt and chart text
are not extracted (python-pptx does not extract them either). Relationships with an external
target, or one that leaves the archive, are ignored, and a slide that cannot be resolved makes the
document unreadable.

Archive limits (`documents/archive.py`), every one a `DOCUMENT_UNREADABLE`:

| Limit                                  | Value  | Why                                                                                                                                                                                 |
| -------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Entries                                | 10,000 | A 300-slide deck has about 1,500. Counted from the central-directory signatures before `zipfile` indexes the archive: 200,000 empty entries in 16 MiB cost 109 MiB and 5 s to index |
| Size of one part read                  | 8 MiB  | Slide XML is 10–200 KB; an extreme 1,000-shape slide is about 2 MB                                                                                                                  |
| Size of all parts read                 | 64 MiB | Also bounds one large slide listed again and again                                                                                                                                  |
| Inflation ratio of a part over 100 KiB | 100:1  | Apache POI's `ZipSecureFile` defaults (0.01 minimum inflate ratio, 100 KiB grace); XML compresses 5–20:1                                                                            |

Media and embedded files are never inflated. Every part is read with `read(cap + 1)`; CPython also
stops at the size the directory declares and checks the CRC, and rejects overlapping entries, so a
header that understates a part's size can only cut it short. XML that declares a DOCTYPE or an
entity is refused outright (OOXML never has one), which rules out entity expansion on top of
expat's own amplification limits.

The adversarial fixtures are built in code (`tests/document_files.py`): a 256 MiB honest zip bomb
in under 1 MiB, a part that lies about its size, parts that together pass the archive limit, a
slide listed 500 times, a 256 MiB Flate stream, a page tree past its limit and a one-page form
replay. The bomb tests also assert peak memory with `tracemalloc`.

### Generating from a document

`POST /v1/generations` takes a `documentId` instead of a `topic`: exactly one of the two, checked by
the transport schema and again by `GenerationRequest` itself (`InvalidGenerationRequestError`
otherwise). Everything after admission is the same job: queue, polling, cancellation, stale-job
sweep, retention, the generation quota (an upload spends the upload budget once; a generation from
it spends one generation unit, like any other job), and the cache.

**Admission.** Before the quota is touched, `CreateGeneration` reads the document through
`DocumentStore.get`, scoped to the requesting `X-Client-Id`. Pending, foreign, missing and expired
documents all return `None` there and the request is `404 DOCUMENT_NOT_FOUND`, the same answer
`GET /v1/documents/{id}` gives, so it does not reveal which case applied. The document is then
claimed in the same transaction that stores the job (`PostgresJobStore.add`): an `UPDATE documents
SET used_at = COALESCE(used_at, now)` with the same conditions, and the job row's `document_id`
foreign key. If the claim matches no row (the document was swept between the read and the insert),
the transaction rolls back, the quota unit is given back and the request is `404`. If the
idempotency key already names another job, the claim is rolled back with the insert, so a replay
never marks a document it did not use. `used_at` is the first use; it is kept for audit and is not
what retention looks at.

**Retention after use.** `generation_jobs.document_id` references `documents` with `ON DELETE
RESTRICT`, and a document is expired when it was uploaded more than
`DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS` ago and no job refers to it
(`is_expired` in `infrastructure/documents/store.py`). Admission and `GET` use the exact complement
(`is_usable`: uploaded within the retention window, or referenced by a job). So a used document
lives as long as the last job that used it, and is swept on the next run after job retention
purges that job: `max(upload + document retention, last job finished + job retention)` plus one
sweep interval. A document used once is gone 24 to about 48 hours after upload with the shipped
settings, depending on how late in its first day it was used.
There is no fixed ceiling: while a job still refers to a document, the client can generate from it
again, and each new job extends its life by another job retention. That only happens through use
the client pays for in generation quota; a document nobody uses again always goes. The sweep holds its batch
`FOR UPDATE`, which both the claim and the foreign-key check conflict with, so a document the sweep
may delete can never gain a reference while it does; an admission whose clock still sees such a
document as usable waits for the sweep and then gets `404`. A `CASCADE` key was rejected because it
would delete the jobs, `SET NULL` because a time-based rule would let a document go while a job
still needed it; the foreign key is the database guaranteeing the one property that matters.

**Pipeline.** `RunGeneration` routes `retrieving_sources` and `parsing_sources` through
`DocumentSourcing` instead of the web retriever and parser: `retrieving_sources` reads the
document's record (as the client that asked) and its object (`ResilientDocumentStorage.get`, capped
at the recorded size); `parsing_sources` runs `DocumentTextParser` and then screens the material
(below). Generation, content filtering, media, finalizing and caching are shared. The generator's
prompt names the uploaded document as its subject instead of a topic, and the fallback deck title is
the document's file name (`subject_of` in `card_generator/generator.py`, the subject passed to
`ContentModerator.screen`). `DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS` (40000, between 50 and 200000) and `DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS` (30, at most 120) configure the parser.

Failure codes on this path: `DOCUMENT_NOT_FOUND` when the record or the object is gone by the time
the worker reads it (a document cannot expire under a job, so this means the object was deleted
outside the sweep, by hand, by a lifecycle rule shorter than the bound above or by a storage
restore; retrying cannot help, which is why it is not `GENERATION_FAILED`), `PROVIDER_UNAVAILABLE`
for a storage or screening outage, the three `DOCUMENT_*` parser codes, and `DOCUMENT_REJECTED`.

**Moderation without a topic.** Two layers. At admission the topic check (`LlmTopicModerator`)
screens what the user wrote: `{"document": <file name>, "instructions": …}` instead of `{"topic":
…}`. The file name has to be screened there because it reaches the result as the citation title and
can become the deck title, which the content filter does not see. In the worker,
`LlmMaterialModerator` screens the whole extracted text, every page or slide numbered, in one call
before any of it reaches the card generator, and a block fails the job `DOCUMENT_REJECTED` (the unit
is spent, like a topic rejection). The whole text rather than an excerpt: it is already bounded by
the character budget, so one call to the cheap moderation model covers it, and a sample would leave
exactly the pages it skipped unscreened. On the topic path the screened topic bounds what search
returns; an upload is arbitrary user content whose file name says nothing about it, and without this
step it would reach the generation provider unscreened. The generated cards still go through the
content filter. The call uses the filter's retry policy and deadline, its own breaker and provider
operation (`document_moderation`), and the moderation concurrency gate.

**Cache.** A document job is cached like a topic job, keyed by `DocumentGenerationFingerprint`
(document id, language, card count, difficulty, sorted note types, images, normalised instructions)
under its own prefix, `deckly:generation-result:document:v1`, so topic keys are byte-for-byte what
they were and the two can never collide. A document's content never changes and its id is
unguessable and owned by one client, so a hit can only serve the client that uploaded it.

**Job timeout.** The budget check is per path. A topic job needs search, model, media, media judge
and filter deadlines plus two gated stages; a document job needs the download, parse and screening
deadlines instead of search, plus three gated stages (`TOPIC_GATED_STAGES_PER_JOB`,
`DOCUMENT_GATED_STAGES_PER_JOB`). With `.env.example`: 335 + 120 = 455 s and 375 + 180 = 555 s, so
`DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS` is 570. Every deployment needs the new value, or the
worker and the API refuse to start. A job from a recording is a third path with its own timeout on
its own worker (`DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS`, see "Transcription").

**Observability.** Lifecycle lines (`generation_admitted`, `generation_started`,
`generation_stage_entered`, `generation_succeeded`, `generation_failed`, `generation_abandoned`)
carry `source_kind` (`topic`, `document` or `recording`) and, for either of the last two, `document_id`;
`generation_document_not_found`, `document_fetched`, `document_parsed` and `document_screened`
cover the document steps. The admission, job and stage metrics have a `source` label, the run and
stage spans a `deckly.source_kind` attribute.

**Worker memory** (`DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES`, default 1280 MiB). A
document-sourced job holds its whole document in the worker's memory, and the worker runs up to
`WORKER_MAX_JOBS` (10) jobs at once, so the same cross-check as the upload gate (DEC-81) covers it:
the process refuses to start when 10 x the peak of one document-sourced job exceeds the budget, and
the error names the terms and the two settings to change. The check lives in `LimitSettings`, so the
API and the worker both run it.

_The peak of one job_ is the larger of two phases that do not overlap. **Download:** the object is
read into a `bytearray` and then copied to `bytes`, so for a moment there are two copies of the
document (50.2 MiB for a 25 MiB object, `tracemalloc`, measured in this repository):
`DOCUMENT_DOWNLOAD_PEAK_COPIES` = 2. **Parse:** the document stays resident while the parser works,
and the parser's own working set does not depend on the document's size, only on its caps: a PDF
whose page content inflates to the 64 MiB limit peaked at 80.5 MiB with a 0.3 MiB file (the 64 MiB
plus one 16 MiB stream being decoded), and a PPTX reads at most 64 MiB of parts in total. The model
takes the larger, 80 MiB (`DOCUMENT_PARSE_WORKING_SET_BYTES`, taken from the parser's own limits so
it moves with them). With the default 25 MiB limit the parse phase is the larger one: 25 + 80 =
105 MiB per job, 1050 MiB for ten. The download phase only dominates above 80 MiB. **A third term
since DEC-84:** two copies of the recording size limit, for the day a worker downloads a recording
the way it downloads a document. At the 48 MiB default that is 96 MiB, under the 105 MiB parse
peak, so it changed nothing; DEC-86 moved it to the transcription worker's own check, because the
generation worker never downloads a recording (see "Transcription" → "Memory").

_Why a separate budget._ The API and the worker are different processes, and in a deployment they
get different container limits, so one shared setting would be wrong for one of them. The default
is an assumption, like DEC-81's, because there is no deployment to measure: a 3 GiB worker
container, about 300 MiB of baseline (the Arq worker, the model, search, media and storage
clients, image thumbnails during media fetching) and half of what remains kept free for the
other stages and for garbage collection, about 1.3 GiB, rounded down to 1280 MiB. It is not read
from the container; set it to what the worker can actually spare. The model is a worst case:
all ten slots must be document jobs, each at the size limit, each at its parse peak at the same
moment, so a typical mix of topic jobs and small decks uses a fraction of it. **The check assumes a
single worker process.** The figures are per process, so with several workers sharing one container,
divide the budget by their number yourself.

Raising `DECKLY_LIMIT_DOCUMENT_MAX_BYTES` moves both checks. With the default worker budget the
largest limit that passes is 48 MiB (48 + 80 = 128 MiB x 10 = 1280 MiB), and the API's upload check,
at 4 document slots next to the recordings' 2 x 48 MiB, stops it at 26 MiB first.

**Parsing concurrency is not bounded, on purpose (DEC-82).** Parsing runs in a thread
(`asyncio.to_thread`) in the worker's process, and pypdf and the XML parsing are pure Python, so
concurrent parses share one interpreter lock with each other and with the worker's event loop, which
runs every other job's I/O. Ten document jobs parsing together could therefore delay the other
jobs' progress and their deadline checks (the parse deadline is wall-clock, so a starved parse
fails sooner rather than later, with `DOCUMENT_UNREADABLE`). The mitigations would be a semaphore
limiting concurrent parses or a process pool, and neither is built: a pool is a pattern the
service does not have yet (separate processes, their own memory, pickling a document across the
boundary, crash handling), and there is no evidence the problem exists. There is no production
traffic, a parse is short next to the model, media and moderation stages of the same job, and
per-user limits (20 jobs a day, 10 uploads an hour) keep the number of document jobs small. What
is covered is the memory, above, since that fails hard and silently at start-up instead of
as slowness. **Revisit this** if real usage shows document-heavy load degrading the worker: the
`deckly_generation_stage_duration_seconds` histogram has a `source` label, so look for the
`parsing_sources` stage of `document` jobs getting slower as concurrency rises, for the stages of
`topic` jobs getting slower while documents are being parsed, or for `DOCUMENT_UNREADABLE` failures
whose message names the extraction deadline. A semaphore on the parse step
is the cheaper first move, a process pool the second.

**Not done here.** The mobile app's contract types, Zod schema and mock transport still describe a
topic-only request; DEC-77 (calling `POST /v1/generations` with a `documentId`) regenerates them.
Note regeneration on a document-sourced deck still searches the web for a topic (DEC-83).

### Object storage client

`infrastructure/storage/` talks to the store over raw `httpx2` with its own AWS Signature V4 signer
(`storage/signing.py`, stdlib `hmac`/`hashlib`), path-style (`{endpoint}/{bucket}/{key}`), which
Cloudflare R2, AWS S3 and S3Proxy all accept. Switching provider is configuration only:
`DECKLY_PROVIDER_STORAGE_ENDPOINT_URL` (`https://<account>.r2.cloudflarestorage.com` for R2,
`https://s3.<region>.amazonaws.com` for AWS), `DECKLY_PROVIDER_STORAGE_REGION` (`auto` for R2), the
bucket and the key pair. Only `PUT`, `GET` and `DELETE` of single objects are used; the payload is signed
with its SHA-256, so the store rejects a body altered in transit. The signer is tested against the
four signatures AWS publishes for its SigV4 examples, and the integration suite runs against
S3Proxy with signature checking on, including a wrong-secret case that must be refused.

Every call goes through `ResilientCaller` with its own operations (`document_upload`,
`document_delete`, `document_download`), breakers and policies: uploads get a long attempt timeout
and deadline (30 s, 60 s) sized for 25 MiB documents and still ample for 48 MiB recordings from a
server's uplink (a 30 s attempt needs about 13 Mbit/s to the store), deletes a short one (5 s, 10 s)
so a sweep batch fits its interval. Network errors, `408`/`409`/`429`/`5xx` (S3's `SlowDown` is a
`503`) are retried with jittered backoff; any other status, a `403` for a bad key above all, is a
persistent failure that counts toward the breaker and is not retried. A `404` on delete counts as
deleted. Either way the API answers `503 UPSTREAM_UNAVAILABLE`, and the provider's error body is
never logged or returned.

This is the same choice DEC-8 and DEC-9 made for Tavily and Commons, and it adds no dependency.
Alternatives that were rejected:

- **aioboto3 / aiobotocore.** Async, and the most complete S3 client. aiobotocore pins botocore to
  a narrow window (3.9.2: `>=1.43.101,<1.43.107`), so it fights every other package that wants
  botocore, the dependency tree is large, and the types come from a separate `types-aiobotocore`
  package with a lot of `Any`, which `disallow_any_unimported` would fight.
- **boto3 behind `asyncio.to_thread`.** Synchronous: a timed-out call keeps its thread, which breaks
  the bounded-call posture the job store has (a call that times out must stop holding resources).
- **obstore** (Rust `object_store` bindings). Async-native and typed, but still `4 - Beta` with one
  maintainer, ships its own retry layer that would stack on `ResilientCaller`, reports errors in
  coarse classes that do not separate a bad key from an outage, and it was not clear that cancelling
  the asyncio task cancels the request on the Rust side.

New dependency: **`python-multipart`** (0.0.32), the parser Starlette's `request.form()` requires.
It is maintained by the Starlette maintainers, pure Python, with no dependencies of its own, and
`make audit` covers it.

Local development uses **S3Proxy** (`andrewgaul/s3proxy:4.1.1`, filesystem backend, SigV4 checking
on) in `docker-compose.yml`, which creates the `deckly-documents` bucket on start. MinIO's community
edition is no longer published as an image, which is why it is not used. The integration tests
create the bucket themselves (a signed `CreateBucket`), so CI needs only the service container.

## Recordings

`POST /v1/recordings` (DEC-84) stores an audio or video recording for transcription (DEC-86) to
read later. It is the same `UploadDocument` use case wired a second time, into the same `documents`
table, object store, upload rate limit, `GET /v1/documents/{id}` and retention sweep, so a recording
is referred to by its `documentId` like any upload. What differs is wired separately: its own
concurrency gate, size limit, read deadline and content probe (`infrastructure/recordings/`), which
also reads the duration (`documents.duration_seconds`, migration `3f8a1c6d9e27`, present in the
response as `durationSeconds`). `POST /v1/generations` with a recording's `documentId` is
transcribed by the transcription worker (DEC-86, see "Transcription"). With
`DECKLY_TRANSCRIPTION_ENABLED=false` it is `422 DOCUMENT_NOT_SUPPORTED_FOR_GENERATION` instead, decided
with the document lookup before the quota is touched (`generation_document_not_supported`, admission
outcome `document_not_supported`), and the document parser refuses a format it has no reader for
instead of raising `KeyError`.

A separate endpoint rather than more types on `POST /documents`: the transport enforces the body
limit and takes a gate slot before it knows what the file is, so one endpoint would force one size
limit and one gate, sized for video, on every PDF upload.

### Formats

| Format | Where people have it                                                            | Detected as       |
| ------ | ------------------------------------------------------------------------------- | ----------------- |
| M4A    | iPhone Voice Memos, Android recorder apps, Zoom's audio-only file, iTunes audio | `audio/mp4`       |
| MP3    | Podcasts, downloaded lectures, older recorders                                  | `audio/mpeg`      |
| WAV    | Dictaphones and field recorders, desktop recording software                     | `audio/wav`       |
| MP4    | Android camera, Zoom, Meet and screen recordings, messenger-compressed video    | `video/mp4`       |
| MOV    | iPhone camera, QuickTime and macOS screen recordings                            | `video/quicktime` |

M4A, MP4 and MOV are one container family (ISO base media / QuickTime), so one parser covers three
of the five. Left out, each a `415`: **Ogg/Opus** (Telegram and WhatsApp voice notes; the most
likely next format, and cheap to probe, the last page's granule position is the length),
**WebM** (Chrome's MediaRecorder writes no Duration element, so there is nothing cheap to read),
**3GP/AMR, raw ADTS `.aac`, FLAC** (rare, or no duration header), and **QuickTime files without an
`ftyp` box** (pre-2005; every iPhone writes `ftyp qt  `). **Fragmented MP4** (DASH, what YouTube
serves) is accepted since DEC-85, with AAC audio only (see "Fragmented MP4" below).

### Sniffing and the duration probe

The probe reads bytes, never the name or the declared type, in a thread, and **never decodes
audio**: a test replaces the audio payload of a real M4A with random bytes and gets the same answer.
It is plain standard-library parsing in the API process, with no new dependency. ffprobe was
rejected because it would put ffmpeg's C code on the request path for untrusted files, and a pipe
cannot seek to a `moov` box at the end of a file. mutagen is GPL-2.0-or-later. tinytag (MIT) picks
its parser from the file extension first, which is exactly what sniffing must not do.

**MP4 / M4A / MOV** (`recordings/isobmff.py`). The file must start with an `ftyp` box whose major or
compatible brands include an accepted one (`M4A `, `M4B `, `qt  `, `isom`, `iso2`–`iso6`, `mp41`,
`mp42`, `avc1`, `M4V `, `MSNV`), and whose major brand is not FairPlay (`M4P `) or an image (`heic`,
`mif1`, `avif`, …). Boxes are walked by their 32- or 64-bit sizes; a box that runs past its parent
is refused, which also catches truncated uploads. There must be exactly one `moov`, `moof` boxes only
when it has an `mvex`, and at least one `soun` track whose first sample entry is not encrypted
(`enca`, `drms`). The duration is the largest of the movie header, each audio track's media header
and the sum of its time-to-sample table (`stts`), which is what a decoder actually follows. Format:
`qt  ` major brand is MOV, otherwise MP4 with a video track and M4A without one.

**MP3** (`recordings/mpeg_audio.py`). Up to four leading ID3v2 tags are skipped by their syncsafe
sizes, then up to 64 KiB of zero padding. The rest must be an unbroken chain of MPEG layer III frame
headers at one sample rate; the duration is the frame count times samples per frame. The Xing/Info
frame count is never trusted, since it can understate the stream. After the last frame only zero
padding, a truncated frame, an ID3v1 tag (exactly the last 128 bytes) and at most 8 KiB of APEv2 or
appended ID3v2 tags may follow. Anything else is a `415`: ffmpeg resyncs past junk and would decode
frames hidden behind it.

**WAV** (`recordings/wave.py`). `RIFF`/`WAVE`, a walk of at most 256 chunks to `data`, exactly one
`fmt ` before it, PCM (8/16/24/32-bit) or float (32/64-bit), plain or `WAVE_FORMAT_EXTENSIBLE`, 1–8
channels at 8–384 kHz, with a consistent block align and byte rate. The duration is every byte after
the `data` header divided by the byte rate, whatever the declared size says, so understating it
gains nothing. RF64 and compressed WAV are `415`.

**Bounded work.** 4,096 boxes, 64 tracks and 1,048,576 time-to-sample entries per file (shared by
the tracks), and every walk that accumulates duration stops as soon as it passes the cap. So the
cost depends on the cap, not on the file. Measured in this repository: a 48 MiB MP3 of the shortest
possible frames is refused after 31 ms at the 15-minute cap (0.4 s at the 4-hour ceiling), and a
1M-entry time-to-sample table takes 94 ms with no allocation beyond the file. The adversarial tests
assert under 0.25 s and under 1 MiB of `tracemalloc` peak.

**Hardened against ffmpeg itself (found in DEC-84's bug hunt).** A probe is only as good as its
agreement with the decoder that runs later, so each case below was built and run through ffmpeg 8.0
before it was fixed:

| Crafted file                                                | Probe said | ffmpeg decoded   | Now                                    |
| ----------------------------------------------------------- | ---------- | ---------------- | -------------------------------------- |
| MP3 frames inside an 834 KB trailing APEv2 or ID3v2 tag     | 1.04 s     | 53.3 s           | `415`, trailing tags over 8 KiB        |
| `stts` timing fewer samples than `stsz` stores              | 0.06 s     | 2.05 s           | `415`, the tables must agree           |
| Second `fmt ` chunk with a 768x higher byte rate            | 0.005 s    | 4.0 s            | `415`, one `fmt ` only                 |
| Two `stts` in one track (any duplicate box the probe reads) | the first  | refused the file | `415`, one of each box the probe reads |

The first three scale with file size: a 48 MiB file could hide hours. The fourth is a parser
differential rather than a measured bypass; refusing it removes the question of which copy wins.
Edit lists (`elst`) that repeat the media were tested too and do not amplify.

**Fragmented MP4** (`recordings/fragments.py`, DEC-85). Each `moof`'s `traf` is matched to its track
by the `tfhd` track id (a fragment of an undeclared track is refused), and each `trun` adds its
samples: per-sample durations when present, otherwise the `tfhd` default, otherwise the track's `trex`
default; samples with no duration from any of the three are refused. A sound track's length is its
`moov` samples plus every fragment's, and the movie-extends header (`mehd`) joins the candidates the
duration is the largest of. `sidx` is an index and is ignored. Fragment boxes have their own budget,
16,384, which is about 1,600 fragments as YouTube lays them out (ten boxes each) and so about 4.5
hours of its 10-second fragments, above the 4-hour duration ceiling; a flood of empty `moof`s is
refused in 11 ms, and the walk still stops as soon as a track passes the cap. A fragmented recording's audio must be
AAC (below), because fragments are where ffmpeg decodes every packet whatever the timestamps say.

**The AAC packet floor (DEC-85).** DEC-84 left one case open: a time-to-sample delta of 1 on AAC
packets (1,024 samples each) makes the container claim 1/1024 of the real length. DEC-85's bug hunt
found it worse in fragments, where **ffmpeg's CLI decodes all of it** (a fragmented file with `tfhd`
defaults of 1 probed at 0.002 s and decoded 2.11 s). It is now closed for AAC, in fragmented and plain
files alike: every AAC track is at least `packets x 960 / sample rate` long (960 is the shortest AAC
frame; 480 for the low-delay object types 23 and 39), so the claim can understate the decoded length
by at most 1024/960. The rate and the codec come from the `esds` AudioSpecificConfig, which is what
the decoder reads, never from the sample entry: the bug hunt showed a sample entry claiming 65,535 Hz
on 16 kHz audio (probe 0.48 s, decoded 2.11 s) and a sample entry relabelled `alac` over AAC data
(probe 0.002 s, decoded 2.11 s, because ffmpeg picks the codec from `esds`). An `mp4a` entry without a
readable `esds` is now `415`, as is a reserved or out-of-range (7.35–96 kHz) frequency. `esds` is
found after the sample entry's fixed fields for sound description versions 0, 1 and 2, directly or in
QuickTime's `wave` atom (iPhone MOV files keep it there).

**The probe is still a gate, not the guarantee.** Codecs other than AAC in a plain (unfragmented) MP4
or MOV, MP3-in-MP4 for one, have no floor, so the probe alone leaves DEC-84's open case for them.
DEC-86 measured it and closed it downstream: such a file passes the probe, and both `ffmpeg -t` and
PyAV decode all of it (`-t` follows the same lying timestamps), so transcription is bounded by decoded
bytes instead, whatever the codec (see "Transcription" → "PyAV").

### Duration cap

`DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS`, default 900 (15 minutes), 1 to 14,400. It was a
compute assumption made before DEC-86 existed, stated so DEC-86 could confirm or correct it. **DEC-86
measured it and kept the cap** ("Transcription" has the figures): RTF 0.11–0.25 on two cores rather
than 0.5, 1.0–1.9 GiB rather than 1.5 GB depending on language and beam (1.03 GiB as shipped), and
913–923 characters a minute rather than 870. The original reasoning, for the record:

- **Engine:** faster-whisper `small`, int8, multilingual because `ru` is a target. `base` is too
  weak outside English; `medium` and `large-v3` are roughly 2–3x slower than `small` on CPU.
- **Speed:** faster-whisper's README measures 13 minutes of audio in 1 min 42 s on 8 threads of an
  i7-12700K, a real-time factor of 0.13. A 4-vCPU cloud worker has half the threads, and a vCPU is a
  hyperthread of a slower server core, so this README assumes about 0.5. That is an estimate for
  DEC-86 to measure, not a measurement. 15 minutes of audio is then about 450 s of CPU, which already
  does not fit beside the current 570 s job timeout's other deadlines: transcription needs its own
  stage budget.
- **Memory:** the same README measures about 1.5 GB of RAM for one `small` int8 run, more than the
  worker's whole 1280 MiB document budget. Transcription needs its own process or a gate of one.
- **Text:** lectures run at about 145 words per minute (a corpus study of Yale lectures), about 870
  characters of transcript a minute, so 15 minutes is about 13,000 characters, a third of
  `DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS` (40,000). That budget fills at about 45 minutes, so
  raising the cap past that is pointless without raising the character budget too.

**Designed to be raised.** The cap exists because transcription is assumed to be self-hosted on CPU.
Once DEC-86's paid-API path removes that, raise it as ordinary config, keeping the text budget above
in mind. It is a setting with a 4-hour ceiling, not a constant, for that reason. A paid API brings
its own limits (OpenAI's transcription endpoint takes at most 25 MB per request, so long files need
chunking or re-encoding there).

### Size limit and memory

`DECKLY_LIMIT_RECORDING_MAX_BYTES`, default 48 MiB, at most 100 MiB. What 15 minutes weighs:

| Source                                   | Per minute        | 15 minutes   | Fits 48 MiB           |
| ---------------------------------------- | ----------------- | ------------ | --------------------- |
| MP3 320 kbps / M4A 256 kbps              | 2.4 / 1.9 MB      | 34 / 27 MiB  | yes                   |
| iPhone Voice Memo (about 64 kbps AAC)    | 0.48 MB           | 7 MiB        | yes                   |
| WAV 16-bit 16 kHz mono / 44.1 kHz stereo | 1.9 / 10.6 MB     | 27 / 151 MiB | mono yes, CD 4.75 min |
| Zoom local recording (about 200 MB/hour) | 3.3 MB            | 48 MiB       | just                  |
| iPhone 1080p30 HEVC / H.264              | 40–60 / 75–130 MB | 0.6–1.9 GB   | about 1 minute        |

So the limit admits any compressed audio and already-compressed video (Zoom, Meet, screen
recordings, messenger video) at the cap, and refuses raw phone-camera video. That video is
gigabytes, which no mobile uplink sends in reasonable time whatever the server does. The fix for
phone video is an audio-only export on the device (`AVAssetExportPresetAppleM4A` on iOS, a
`MediaExtractor`/`MediaMuxer` remux on Android, neither needs ffmpeg), a frontend follow-up.

The upload is held in memory like a document's (`read_upload`), so the limit is also a memory
figure. The recording gate (`DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY`, default 1 since DEC-85, 2 before, 1 to 8,
metric label `recording_upload`) shares the DEC-78 budget through the summed check described under
"Concurrency cap", which is why document slots went from 8 to 4. A full recording gate refuses
recordings at once with `503` and `Retry-After: 5` and does not touch document uploads. The read
deadline (`DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS`, default 240): a full 48 MiB body
takes about 80 s at 5 Mbit/s, so 240 s is three times that, as for documents. Making video limits
independent of memory would mean streaming the spooled upload to the store instead of reading it
into `bytes`, a rework of DEC-74's upload path left for when video uploads are seen hitting `413`.

**Retention** is unchanged: 24 hours, or as long as a job refers to the recording, swept by the same
task (its delete cost does not depend on object size). Storage cost is the only thing that grows:
worst case per address is 50 uploads x 48 MiB an hour, pennies a month on R2.

### Audio extraction: at transcription time, not at upload

The original file is stored and DEC-86 extracts, or decodes directly:

- faster-whisper can decode containers itself through PyAV, which bundles the FFmpeg libraries, and
  paid transcription APIs accept MP4 and M4A directly. Extracting at upload would have picked an output
  format before DEC-86 picked an engine. (DEC-86 decodes with the ffmpeg program at transcription time
  and never lets PyAV see the file; see "Transcription".)
- The API stays pure Python: no ffmpeg on the request path or in the API image, and upload latency
  does not change.
- The cost is small and lands on DEC-86. Video objects are larger than extracted audio, but for at
  most 24–48 hours; and the worker should stream a recording to a temporary file rather than read
  it into memory as it does a document (the worker memory check's recording term assumes the latter
  for now).

### ffmpeg: a system dependency, for tests and CI only

DEC-84 added no Python package. It did add **ffmpeg as a system binary, then only where tests run**:
the integration suite extracts audio from the MP4 and MOV objects it stored in S3Proxy and checks
the result's length, and checks that extraction stops at the cap however long the input is
(`make test-integration`). **Since DEC-86 ffmpeg is also a runtime dependency of the transcription
worker**, and those tests run the production decoder (`tests/ffmpeg.py` only builds it). Locally that needs `brew install ffmpeg` (8.0 at the
time of writing). CI's `ubuntu-24.04` image does not include ffmpeg, so the backend job now runs
`apt-get install --no-install-recommends ffmpeg` (6.1, signed by the Ubuntu archive) before the
integration tests. The tests do not depend on version-specific output.

The recipe DEC-86 started from, now `infrastructure/transcription/decoding.py` (which dropped `-t` and
bounds the output in bytes instead, for the reasons in "Transcription"):

- An argument list to `asyncio.create_subprocess_exec`, never a shell.
- The demuxer forced from the sniffed type (`-f mov`, `-f mp3`, `-f wav`), so a file cannot switch
  ffmpeg to another format.
- `-protocol_whitelist file`, which closes the playlist-style inputs that make ffmpeg read other
  files or URLs.
- `-nostdin -vn -sn -dn -map_metadata -1 -threads 1`.
- `-t <cap>` plus, per the probe section above, a bound on decoded samples.
- A wall-clock timeout that kills the process.

Memory should be capped by the container's cgroup or `prlimit`, not `preexec_fn`, which Python
documents as unsafe in a process with threads. No wrapper library: ffmpeg-python is unmaintained and
untyped, and python-ffmpeg adds nothing over a dozen arguments. **Docker:** there is no backend
image in this repository yet (`docker-compose.yml` only runs Postgres, Redis and S3Proxy). Whichever
worker image DEC-86 adds must install ffmpeg (Debian `ffmpeg`) or rely on PyAV's bundled libraries;
the API image needs neither.

### Fixtures

Real files from real muxers live in `tests/fixtures/recordings/` (92 KiB in all). The adversarial
ones are built in code (`tests/recording_files.py`). The real ones were made on macOS with ffmpeg 8.0
(`-bitexact`, `lavfi` sine and test-pattern sources) and Apple's own tools, so Apple's muxer is
covered too:

```bash
q=(-hide_banner -loglevel error -y -bitexact)
ffmpeg "${q[@]}" -f lavfi -i sine=frequency=440:sample_rate=22050:duration=2 -ac 1 -c:a libmp3lame -b:a 32k tone.mp3
ffmpeg "${q[@]}" -f lavfi -i sine=frequency=440:sample_rate=16000:duration=2 -ac 1 -c:a aac -b:a 24k tone.m4a
ffmpeg "${q[@]}" -f lavfi -i sine=frequency=440:sample_rate=8000:duration=1 -ac 1 -c:a pcm_s16le tone.wav
ffmpeg "${q[@]}" -f lavfi -i testsrc=size=32x32:rate=5:duration=2 -f lavfi -i sine=frequency=440:sample_rate=16000:duration=2 \
  -c:v libx264 -preset ultrafast -crf 51 -pix_fmt yuv420p -c:a aac -b:a 24k -ac 1 -shortest clip.mp4   # and -f mov clip.mov
ffmpeg "${q[@]}" -f lavfi -i testsrc=size=32x32:rate=5:duration=2 -c:v libx264 -preset ultrafast -crf 51 -pix_fmt yuv420p silent.mp4
afconvert -f m4af -d aac source.wav apple.m4a        # source.wav: the 44.1 kHz sine above
avconvert --source clip.mp4 --preset PresetLowQuality --output apple.mov
sips -s format heic red.png --out red.heic           # red.png: one 16x16 frame of lavfi color=c=red
ffmpeg "${q[@]}" -f lavfi -i sine=frequency=440:sample_rate=16000:duration=2 -ac 1 -c:a aac -b:a 24k \
  -movflags +dash+global_sidx -frag_duration 500000 -f mp4 tone-fragmented.m4a   # DEC-85, DASH like YouTube
ffmpeg "${q[@]}" -f lavfi -i testsrc=size=32x32:rate=5:duration=2 -f lavfi -i sine=frequency=440:sample_rate=16000:duration=2 \
  -c:v libx264 -preset ultrafast -crf 51 -pix_fmt yuv420p -c:a aac -b:a 24k -ac 1 -shortest \
  -movflags +frag_keyframe+empty_moov+default_base_moof -f mp4 clip-fragmented.mp4
```

### Not done here

- **Frontend:** the mobile app does not call `POST /v1/recordings`. Its generated contract types
  are regenerated, but the Zod schemas and the mock transport do not know the endpoint. Audio-only
  export of phone video belongs there too.
- **Formats:** Ogg/Opus first. Fragmented MP4 is done (DEC-85).
- **Larger video:** streaming uploads to disk (see "Size limit and memory").

## Recordings from a URL

`POST /v1/recordings/from-url` (DEC-85) takes `{"url": "<YouTube link>"}`, fetches the video's
audio-only M4A stream and stores it as a recording. It is `UploadDocument` wired a third time, with a
fetch in place of the request body, so the result goes through the same size check, the same duration
probe, the same `documents` row, object key, retention and sweep as an upload. Nothing downstream can
tell which path produced a `documentId`; DEC-86 needs no branch for it. The contract is in
`api-contract.md`; the code is `domain/recording_url.py` (the link) and
`infrastructure/recording_url/` (the egress proxy, the yt-dlp runner and the fetcher).

### Production caveat: YouTube blocks datacenter addresses

**This feature may not work from a cloud server at all.** YouTube answers many datacenter and VPN
addresses with "Sign in to confirm you're not a bot", and as of this writing the PO-token workarounds
no longer get past it in most cases (bgutil-ytdlp-pot-provider's own notice). The fixes that work are a
residential or otherwise trusted egress address, or a logged-in account's cookies; this service uses
neither (cookies would tie every user's fetch to one Google account, against its terms and at the risk
of that account). It fails cleanly instead: the request is `503 UPSTREAM_UNAVAILABLE` with
`Retry-After: 60`, logged as `recording_url_fetch_failed` with `outcome="blocked"`, and counted in
`deckly_recording_url_fetches_total{outcome="blocked"}`. **Alert on that series** after deploying.
`make test-live` proves the feature from a developer's machine (two tests, about 5 seconds); it says
nothing about the production address.

### Terms of service

Downloading from YouTube is restricted by YouTube's Terms of Service, which allow downloads only where
YouTube offers a download button or link, and other platforms have their own terms. This is not a
legal opinion, only a statement that the constraint exists. It has to be reviewed before this endpoint
is exposed to real users, like the other pre-production notes in this README.

### Downloader: yt-dlp

Researched to the same depth as DEC-8 (Tavily), DEC-9 (Commons) and DEC-84 (the probe):

- **License.** yt-dlp is under the Unlicense (public domain). Its README states that the PyPI wheel
  contains only Unlicense code; the GPLv3+ applies only to its PyInstaller executables, and the
  `[default]` extra would pull in `mutagen` (GPL-2.0-or-later), which is why it is not installed.
- **Maintenance.** Very active: 14 stable releases between October 2025 and August 2026, nightly
  builds most days. It has to be: YouTube changes often enough to break extractors every few weeks,
  so **the pin needs regular bumps** (`yt-dlp==2026.8.19` today). `make test-live` is the check to run
  after a bump.
- **Sites.** About 940 extractor modules, including a generic extractor that follows any URL, which
  is exactly the SSRF surface this endpoint must not have. Only YouTube is enabled
  (`--use-extractors youtube`, matched as a full regex against extractor names, so it loads exactly
  one extractor). Adding a site means a link parser, a host list and its own review, never the generic
  extractor.
- **Subprocess, not the Python API.** yt-dlp documents itself as a CLI first. Its Python API is
  untyped (no `py.typed`, which `disallow_any_unimported` refuses), synchronous (a timed-out call keeps
  its thread, the reason DEC-74 rejected boto3), and would run untrusted network I/O and YouTube's
  JavaScript inside the API process. A subprocess can be killed, and its network can be confined.
- **JavaScript.** Full YouTube support needs `yt-dlp-ejs` and a JavaScript runtime to solve YouTube's
  player challenges; deno is the default and the one used. yt-dlp runs it with `--no-remote --no-prompt
--no-config` and without `--allow-net`, so the solver has no network or file access.
- **Rejected:** youtube-dl (effectively unmaintained), pytubefix (single maintainer, in-process,
  breaks often), and third-party download APIs (they would receive users' links, with worse terms).
  yt-dlp can also fetch YouTube's own captions; DEC-90 measured whether they should replace
  transcription and decided not yet (see "Transcription" → "YouTube captions instead of transcription").

**New dependencies:** `yt-dlp` 2026.8.19 (Unlicense, no dependencies of its own), `yt-dlp-ejs` 0.8.0
(Unlicense, bundles MIT and ISC code, no dependencies) and `deno` 2.9.7 (MIT, PyPI's redistribution of
the Deno binary, about 37 MB per platform). All three are exact pins in `uv.lock`, so `make audit`
covers what PyPI advisories cover; that does not include CVEs in the Deno runtime itself. The API
refuses to start without them (`require_runtime`). yt-dlp's optional packages (`requests`,
`urllib3`, `curl_cffi`, `websockets`, `certifi`, `brotli`, `pycryptodomex`, `mutagen`) are not
installed, so it uses its standard-library networking and the system CA store.

### SSRF: three independent layers

1. **The link is reduced to an id.** `parse_youtube_video` accepts visible ASCII only, `http` or
   `https`, a host from `youtube.com`, `www.`, `m.` or `music.youtube.com` or `youtu.be`, no
   credentials, no port, and a path that names one video. It keeps only the 11-character id
   (`[A-Za-z0-9_-]`), and the server builds `https://www.youtube.com/watch?v=<id>` itself. That is the
   only value derived from the client on yt-dlp's command line, after `--`. An allowlist rather than
   general SSRF hardening because the feature needs one platform, and a link the server rebuilds
   cannot point anywhere else. yt-dlp's own YouTube pattern is looser (Invidious instances, hooktube,
   naked ids), which is why the client's link is never passed through.
2. **yt-dlp reaches the network only through an egress proxy.** yt-dlp has no hook to vet
   destinations, and a check before invoking it would miss DNS rebinding, redirects and the media URLs
   on other hosts (`*.googlevideo.com`). But `--proxy` routes every request of every handler through
   one proxy (it replaces the environment's), so each fetch starts its own CONNECT proxy on
   `127.0.0.1`, with a random credential, for its lifetime (`egress.py`). It accepts only `CONNECT`
   to port 443 of `www.youtube.com`, `youtube.com`, `m.youtube.com`, `music.youtube.com` and
   `*.googlevideo.com` (checked before any DNS lookup), resolves the host once, refuses unless **every**
   answer is a public address (no loopback, private, link-local, metadata, CGNAT, documentation,
   multicast or site-local addresses; IPv6 must be global unicast, and IPv4-mapped, -compatible,
   -translated, NAT64, 6to4 and Teredo forms are judged by the IPv4 address inside them), and
   **connects to the address it checked**, so the answer cannot change between check and use. TLS
   stays end to end. Each redirect opens a new tunnel and passes the same checks, and a plain-HTTP
   redirect target (`http://169.254.169.254/`) is not even a `CONNECT`. The proxy also caps the
   fetch's tunnels (32) and bytes in both directions (the recording size limit plus 16 MiB for the
   pages and player script), which is the real size limit: yt-dlp's `--max-filesize` only checks a
   `Content-Length` (`downloader/http.py`), so a response without one would stream unbounded. The live
   run used `www.youtube.com`, `manifest.googlevideo.com` and one `rr…googlevideo.com` media host.
3. **The deployment should still deny internal ranges** to the API container's egress (RFC 1918,
   link-local and the metadata address), as a third line (see "Production deployment").

The tests demonstrate this rather than assert that an allowlist exists: the real proxy, with a
resolver that answers `127.0.0.1`, `169.254.169.254`, `10.1.2.3`, `::1` or `::ffff:127.0.0.1` for
`www.youtube.com`, refuses the tunnel while a listener on that loopback port records **zero
connections**; a positive control with a public answer reaches the same listener, so the zero means
something. A rebinding resolver (public, then loopback) gets one tunnel to the public address and the
second refused. A redirect chain through a host off the list, an IP literal, an allowlisted host
resolving to the metadata address and a plain-HTTP request to it are each refused, and only the first,
legitimate hop was ever dialled. The integration suite runs **the real yt-dlp** through the real proxy
with YouTube's names poisoned to internal addresses: the fetch fails with `EgressRefusedError` and the
internal listener sees nothing.

### Running yt-dlp

The DEC-84 ffmpeg recipe, applied to a downloader: an argument list to
`asyncio.create_subprocess_exec`, never a shell, `python -I -m yt_dlp` (isolated mode: no user site,
no environment variables, no current directory on the path); `--ignore-config --no-plugin-dirs
--no-cache-dir --no-cookies --no-remote-components`; `--no-js-runtimes --js-runtimes deno:<path>`;
`--use-extractors youtube --no-playlist`; `--format bestaudio[ext=m4a][protocol=https]` (one plain
HTTPS file, no HLS or DASH segments); for the download `--downloader native --fixup never` (no ffmpeg
anywhere); `--retries 1 --extractor-retries 1 --socket-timeout 15`. The environment is only `HOME`,
`TMPDIR` and `DENO_DIR` (a fresh temporary directory that is also the working directory and is
deleted afterwards), `LC_ALL`, `DENO_NO_UPDATE_CHECK` and an **empty `PATH`**, so yt-dlp cannot find
an ffmpeg, aria2c or any other helper even if the host has one. The process starts its own session,
and the deadline (`DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS`, 120 by default, 1 to 600) or a
cancelled request kills **the whole process group**, deno included; stderr is read through a 16 KiB
tail.

### Metadata first, the file second

The fetch runs yt-dlp twice. The probe (`--skip-download --write-info-json`) costs one watch page and
one player request and downloads no audio. Its JSON (at most 16 MiB, validated with pydantic) must
describe the same video id, be `not_live` or `was_live`, have a positive finite duration within the
cap, offer an `m4a` over `https`, and not state a size over the limit; each refusal costs no media
bytes. The download then reuses that file (`--load-info-json`), so it fetches exactly the format that
was checked without a second extraction, with `--match-filters "duration <= cap & !is_live"` and
`--max-filesize` as belt and braces.

Then **the downloaded bytes are probed like any upload** and the probe's duration is what is stored,
never YouTube's. YouTube's figure is YouTube's own encode and rounded to whole seconds, so it is less
likely to lie than an uploaded container, but nothing in yt-dlp promises it. YouTube's audio is
fragmented MP4 (`ftyp dash`, about 10-second fragments), which is why the probe learned fragments, and
why the bug hunt above matters here: a downloaded file with `tfhd` durations of 1 or a mislabelled
sample entry is refused or floored exactly as an uploaded one is. The use-case tests serve a download
that claims 10 seconds in its metadata and plays 120 (`422`, nothing stored) and DEC-84's own lying
files as downloads (`415` or `422`). Because YouTube rounds, a video listed at exactly the cap can
still be refused after downloading, by under a second.

Failures are classified from yt-dlp's last `ERROR:` line, pinned to the yt-dlp version: the bot check
and HTTP 429 are `blocked`; private, removed, age-restricted, region-blocked, members-only, live,
upcoming and format-less videos are `422 RECORDING_UNAVAILABLE`; anything else is `503`. Any tunnel
the proxy refused fails the fetch as `egress_refused` (`503`) whatever yt-dlp printed, and a refused
byte budget is `413 DOCUMENT_TOO_LARGE`. The filename is the video title with path separators made
hyphens, cleaned like an upload's and cut to 200 characters, plus `.m4a`; a title with nothing
printable left becomes the video id.

### Budget, capacity and memory

**Its own, tighter budget**, separate from uploads: 5 fetches per client and 20 per address per hour,
and **200 per hour for the whole server** (`DECKLY_LIMIT_RECORDING_URL_*`, keys
`deckly:recording-url:*`, all three counted in one `MULTI`). Uploads are the client spending its own
bandwidth; a fetch is the server spending its bandwidth, CPU and a subprocess for anyone who asks,
including for a request that is refused after the probe, and every fetch spends the server address's
standing with YouTube, which is shared by every user. The server-wide ceiling is what protects that
standing; the cost is that a client with many addresses can use it up for an hour, which is the same
outcome as a YouTube block, only shorter. A link the parser refuses is not counted; a fetch refused
because the slot is busy is not counted either.

**One fetch at a time per API process** (`DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY`, default 1, at
most 4, metric gate `recording_url`), refused at once with `503` and `Retry-After: 5` like an upload.
The recording gate went from 2 slots to 1 to make room. **yt-dlp and deno together peaked at 394–422
MiB of resident memory during the probe and about 55 MiB during the download**, outside the API's
Python heap but inside its container, so each URL slot adds 448 MiB (the peak rounded up) x 2 to the
startup memory check, which is why the default budget is now 1,288 MiB and the container assumption
about 2 GiB (see "Concurrency cap"). A second URL slot needs 1,288 + (48 + 448) x 2 = 2,280 MiB of
budget; the startup check refuses it until the budget says the container has it. An earlier
measurement in this ticket reported 340 MB: that was `/usr/bin/time -l`, which on macOS reports the
largest single child, not deno and yt-dlp together.

The request is answered when the audio is stored: about 2.5 seconds for a 19-second video and 5 for a
3.5-minute one from a developer's connection, up to the deadline. The reverse proxy's response timeout
must allow that. A client that disconnects does not stop the fetch, which finishes (or hits its
deadline) and stores a document nobody reads, swept like any other.

### Not done here

- **Frontend:** the app does not call the endpoint yet; its generated types are regenerated.
- **Other sites:** one parser and one host list each, reviewed like YouTube's.
- **Cookies, PO tokens or a residential egress** for the datacenter block: a product and legal
  decision, not code.
- **Request body size:** like every JSON endpoint here, the body is read before it is validated;
  the reverse proxy's body limit is what bounds it.

## Transcription

`POST /v1/generations` with a recording's `documentId` (DEC-86) transcribes the recording and
generates from the transcript like any document: the same job, stages, quota, cache, moderation and
result. What differs is where it runs and what it cites. The job goes to its own queue, which only
`deckly-transcription-worker` reads; the worker decodes the recording with ffmpeg into at most the
duration cap of audio, runs faster-whisper in a separate engine process, and turns the transcript
into `SourceMaterial` whose sources are `TranscriptSource` time ranges. The code is
`infrastructure/transcription/`; nothing in `application/pipeline.py` changed, because the
transcriber is a `DocumentParser`: transcription is what `parsing_sources` does for a recording.

### Engine: faster-whisper `small`, int8, greedy

DEC-84 sized the duration cap on estimates. They were measured here, on real speech rather than test
tones: LibriVox recordings, which are public domain, of a 1916 Cambridge lecture in English
("Russian Realities and Problems", lecture 1) and of Turgenev in Russian ("Хорь и Калиныч"),
15-minute clips scored against the books' text (archive.org's OCR for the English, which inflates its
error rate a little, and ru.wikisource for the Russian). The machine is an Apple M3 Pro, both native
and in a Linux arm64 container limited with `--cpus` (Docker Desktop's VM, `python:3.12-slim`),
faster-whisper 1.2.1, CTranslate2 4.8.3, `small` quantised to int8 at load, VAD on. RTF is the real-time
factor, seconds of work per second of audio; memory is the engine process's peak resident set
(`/usr/bin/time` in the container).

| Linux container, 2 vCPU, 120 s chunks | RTF   | 15 minutes takes | Peak memory | Word error rate |
| ------------------------------------- | ----- | ---------------- | ----------- | --------------- |
| English, beam 5                       | 0.153 | 138 s            | 1,316 MiB   | 7.6 %           |
| English, beam 1 (shipped)             | 0.111 | 100 s            | 1,026 MiB   | 7.7 %           |
| Russian, beam 5                       | 0.251 | 226 s            | 1,875 MiB   | 18.3 %          |
| Russian, beam 1 (shipped)             | 0.140 | 126 s            | 1,030 MiB   | 20.4 %          |

- **Beam 1, not faster-whisper's default of 5.** It is 1.4–1.8x faster and halves the Russian peak,
  for no measurable loss in English and two points of word error rate in Russian. The transcript feeds
  a model that writes cards, not a reader, so the trade is cheap. It is a constant
  (`engine.py`, `BEAM_SIZE`), not a setting, because the memory budget below is measured for it.
- **Chunks of 120 s, cut at the quietest 100 ms of their last 10 s.** faster-whisper computes the
  spectrogram and runs VAD over the whole array it is given, so unchunked memory grows with the
  audio: 1.09, 1.29 and 1.81 GiB for 1, 5 and 15 minutes (native, beam 5). Chunking bounds it, for
  about 14 % more CPU, and the cut lands in a pause between words: the chunked English transcript has
  the same word count as the unchunked one and a word error rate of 7.63 % against 7.83 %. The
  language is detected on the first chunk with speech and kept for the rest.
- **The language is detected, never taken from the request.** The request's `language` is the
  language of the cards, which need not be the recording's.
- **Threads.** 1, 2 and 4 threads gave RTF 0.245, 0.162 and 0.110 natively; in the container, 4 vCPU
  was no faster than 2 (0.164 against 0.153). `DECKLY_TRANSCRIPTION_CPU_THREADS` defaults to 2; set it
  to the transcription worker's vCPUs.
- **Text.** 923 characters a minute in English and 913 in Russian (DEC-84 estimated 870), so
  `DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS` (40,000) fills at about 43 minutes. Past that, the rest of
  the transcript is cut like a long document's (`transcript_parsed` logs `budget_exhausted`), and the
  CPU spent on it is wasted, so raising the duration cap past ~40 minutes needs the character budget
  raised with it.
- **Adversarial audio.** Five minutes of white noise cost RTF 0.002 (VAD keeps none of it; 0.038
  without VAD), and two voices talking over each other 0.14, so noise does not multiply the work
  through Whisper's temperature fallback. The deadline below bounds what does.
- **Model load** takes 0.3–1.3 s, so the engine is started per job (below) rather than kept warm.
- **Russian quality is the weak spot** (about one word in five wrong). `medium` would be better and
  2–3x slower. This was accepted for v1; a paid provider (below) is the planned way past it.

**Is it viable at the 15-minute cap? Yes.** The worst shipped case, Russian at 15 minutes, is 126 s on
two M3 cores. A cloud vCPU is a hyperthread of a slower server core; assuming one is half as fast,
that is about 4–5 minutes, inside the 540 s deadline with room to spare. That factor is an
assumption: `deckly_transcription_real_time_factor` measures the deployment's real one (below), and
the deadline should be revisited against it. Throughput is the real limit: one 2-vCPU transcription
worker finishes roughly 8–10 recordings at the cap an hour, so scale it by replicas.

### PyAV, and why the engine is never given the file

DEC-84's comment was right, and the risk is broader than it said. faster-whisper decodes a path or a
file object with `decode_audio`, which runs PyAV's `container.decode(audio=0)` over every packet, with
no cap and no regard for timestamps, and buffers all of it (int16, then a float32 copy) before
transcribing. Handing `transcribe()` a NumPy array skips PyAV entirely, which is what the engine does.
(On PyPI, faster-whisper 1.2.1's `decode_audio` does not even run with PyAV 19, which removed an
argument it passes; the fix is on faster-whisper's main branch but unreleased.)

The bound has to come from somewhere other than metadata. A 41-minute MP3 remuxed into MP4 with its
time-to-sample deltas cut from 1152 to 32, the non-AAC case DEC-84 and DEC-85 left open:

| Who reads it                                  | Says or decodes                                                            |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| The upload probe (`recordings/isobmff.py`)    | 136.6 s, accepted under the 900 s cap                                      |
| `ffmpeg -t 900`                               | 2,459 s decoded: `-t` follows the same lying timestamps, so it never fires |
| PyAV's decode loop (what `decode_audio` does) | 2,459 s decoded, in 1.4 s                                                  |
| A read of ffmpeg's PCM output capped in bytes | exactly 900 s                                                              |

So the earlier note that "ffmpeg's CLI follows their timestamps" was true and was not a protection:
it follows them to the wrong answer. **The decoder is bounded by decoded bytes, and only by them**
(`decoding.py`): ffmpeg writes 16 kHz mono s16le to a pipe, the worker copies at most
`DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS` x 32,000 bytes to a scratch file, reads one byte more to
tell a recording that ended at the cap from one that ran past it, and kills ffmpeg's process group in
the second case. A recording cut that way is logged as `recording_decode_truncated` (with what the
upload claimed and what was decoded) and counted in `deckly_recording_decode_truncations_total`, and is
transcribed up to the cap; no codec needs its own floor. `-t` was dropped from DEC-84's recipe: it bounds
nothing a lie can get past, and an honest recording it cut would end exactly at the cap and never be
counted. The rest of the recipe stands: an argument list, never a shell; the demuxer forced from the
sniffed type; `-protocol_whitelist file`; `-nostdin -vn -sn -dn -map_metadata -1 -threads 1`; an empty
`PATH`; its own session; and a wall-clock deadline (`DECKLY_TRANSCRIPTION_DECODE_DEADLINE_SECONDS`,
30; 15 minutes decodes in 0.42 s at 21 MiB) after which the process group is killed and the job fails
`DOCUMENT_UNREADABLE`. The engine reads at most the sample count it is told, and segment timestamps
are clamped to the audio, so nothing downstream can cite past the cap either. The integration suite
builds this lying file from a real fixture and demonstrates all four rows of the table.

### Isolation: its own queue, its own worker, its own process

The ticket assumed transcription would hold the GIL for the whole job. **It does not**: CTranslate2
releases the interpreter lock while it computes. Measured with a 5-minute transcription running beside
an event loop that times 10 ms sleeps and five small fake generation jobs:

| Beside the event loop      | Native, 4 threads: lag p99 / max, job time | Container, 2 vCPU, 2 threads |
| -------------------------- | ------------------------------------------ | ---------------------------- |
| Nothing                    | 1.3 ms / 7.6 ms, 1.13 s                    | 3.0 ms / 5.6 ms, 1.23 s      |
| Transcription in a thread  | 1.5 ms / 41 ms, 1.11 s                     | 3.2 ms / 83 ms, 1.19 s       |
| Transcription in a process | 2.0 ms / 13 ms, 1.12 s                     | 18 ms / 77 ms, 1.21 s        |

So a semaphore around a thread would not starve the event loop either. What would go wrong is
everything else, and each is why the design is what it is:

1. **Arq slots.** The generation worker runs ten jobs at once. A recording job holds a slot for
   minutes, and any gate in front of the engine makes the jobs waiting at it hold slots too, so a
   burst of recordings fills all ten and topic jobs wait in Redis behind them. Recording jobs therefore
   go to their own queue (`arq:queue:transcription`, `QueueLane.TRANSCRIPTION`), chosen at admission from
   the document's format (`lane_for` in `application/generations.py`; a replay of a queued job looks the
   document up again). Generation workers never see them, and a recording job that reached one anyway
   would fail `DOCUMENT_UNREADABLE`, because the generation worker's parser has no reader for audio.
2. **Transcriptions slow each other.** Two at once on 2 vCPU took RTF from 0.16 to 0.44 and 0.51, so
   each would miss a deadline sized for one. The transcription worker runs **one job at a time**
   (`TRANSCRIPTION_WORKER_MAX_JOBS`, a constant like `WORKER_MAX_JOBS`); others wait in its queue, not
   on its CPU. Scale is more replicas, each in its own container.
3. **A thread cannot be killed.** Arq's timeout and a client's cancel cancel the coroutine, but a
   transcription in a thread would run on, using the CPU while the worker starts the next job: the two
   overlapping transcriptions of point 2. The engine is a child process instead
   (`python -I -m deckly.infrastructure.transcription.engine`, the yt-dlp runner's recipe: isolated mode,
   its own session, scratch `HOME`/`TMPDIR`, empty `PATH`, `HF_HUB_OFFLINE=1`), and any exception or
   cancellation kills its process group (`process.py`). Its stdout is the transcript as one JSON
   document, capped at 16 MiB and validated with pydantic (`engine_output.py`); a crash or the deadline
   fails the job `PROVIDER_UNAVAILABLE`.
4. **Memory.** About 1 GiB per transcription does not fit in the generation worker's 1,280 MiB budget
   at all, so it has its own (below), in its own container.

The worker is the same image and the same composition root (`wire_generation` in `worker/settings.py`)
with a `RecordingTranscriber` as the document parser, its own queue, timeout and metrics port, and no
housekeeping cron. It refuses to start without ffmpeg, without the pinned model, or while
`DECKLY_TRANSCRIPTION_ENABLED` is false. **Deploy it as its own container with its own CPU quota**; that,
not the process boundary, is what keeps its cores away from the generation worker's. Co-located on one
host (local development), the generation worker is still fine, per the table: its work is I/O.

The integration suite demonstrates this rather than asserting it, each with a control that shows the
failure it prevents: generation jobs keep their pace (lag p99 under 50 ms, under 1.5x their solo time)
beside a real transcription in a loop, while the same probe catches an in-process thread that holds the
interpreter lock; three recordings and a topic job on real Arq workers, where the topic job starts at
once and the recordings run strictly one after another, against the same four jobs on one shared queue,
where the topic waits behind three overlapping transcriptions; and a cancelled transcription leaves no
engine process running.

### Timing and the job budget

Transcription needs its own job timeout: a recording job at the cap does not fit the 570 s that topic
and document jobs get, and raising that would let a hung topic job run three times longer. The Arq
timeout is per worker, so the transcription worker has its own
(`DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS`, 960), checked at startup like DEC-68/76's per-path
budgets, as a third path:

| Recording-sourced job                      | Deadline (default) |
| ------------------------------------------ | ------------------ |
| Download (`STORAGE_DOWNLOAD_DEADLINE`)     | 30 s               |
| Decode (`TRANSCRIPTION_DECODE_DEADLINE`)   | 30 s               |
| Transcription (`TRANSCRIPTION_DEADLINE`)   | 540 s              |
| Transcript screening (moderation filter)   | 20 s               |
| Model, media, media judge, content filter  | 295 s              |
| Moderation gate waits (one job per worker) | 0 s                |
| **Total, which must stay under 960 s**     | **915 s**          |

`DECKLY_TRANSCRIPTION_DEADLINE_SECONDS` (540) allows an RTF of 0.6 at the 900 s cap, four times the
worst measured above. A second check refuses a deadline under cap x the measured RTF (0.14) x 2
(252 s at the default cap), so raising the duration cap without the deadline fails at startup instead
of as timeouts. **The stale-job sweep had to move with it:** a running job must not go stale before the
longest job timeout has passed, so `DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS` is now 1,200 (was 900),
and the startup check uses the transcription timeout while transcription is enabled. The cost is that a
topic job whose worker crashed shows as running for up to 20 minutes rather than 15 before the sweep
fails it. A per-source sweep would avoid that and was judged not worth its complexity yet.

**Backlog.** A queued recording goes stale after its own threshold,
`DECKLY_SWEEP_RECORDING_QUEUED_STALE_AFTER_SECONDS` (three hours), not the one-hour threshold of other
queued jobs; see "Generating from a recording". There is no admission backpressure;
`deckly_generation_queue_depth{queue="transcription"}` is reported next to the generation queue's on the
API's `/metrics`. **Alert on it** (more than about eight waiting per replica) and add replicas.

A recording job spends minutes in `parsing_sources` with nothing to show; the stage does not report
progress within itself.

### Memory

`DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES`, default 2,560 MiB (a 3 GiB container less about
512 MiB for the worker process itself). The check is the same shape as DEC-81's and DEC-85's, with
DEC-85's x2 margin:

> 1 job x (recording size limit + 1,024 MiB of engine working set + the cap's decoded audio) x 2

The recording stays in the worker's memory while it is transcribed (the pipeline holds the downloaded
bytes for the length of the parse), the engine's working set is the measured 1,030 MiB peak less the
27.5 MiB of decoded audio it held, rounded up, and the decoded audio is the cap x 32,000 bytes, because
the engine reads it whole before chunking. The download's two copies and ffmpeg's 21 MiB come before the
engine starts, so they never add to it. With the defaults: (48 + 1,024 + 27.5) MiB x 2 = 2,199 MiB. A
one-hour cap still fits (2,364 MiB); the four-hour ceiling does not (3,023 MiB), so the budget must grow
with it. The generation worker's own check lost the recording term DEC-84 added for this day, since it
never downloads a recording now. The scratch directory (the recording, about 28 MiB of PCM at the cap)
must be on disk, not a tmpfs, or it counts against the container's memory too.

### Transcripts and citations

`TranscriptSource(title, document_id, start_ms, end_ms)` is the third `Source` variant
(`domain/source.py`), `{"kind": "transcript", "title", "documentId", "startMs", "endMs"}` on the wire.
Whisper's segments (2–10 s each) are grouped into passages of about a minute, cut where one segment ends
and the next begins (`transcriber.py`), so a 15-minute lecture is about 15 citable entries, as a PDF is a
few dozen pages; each passage is cleaned and budgeted exactly as a page is (`usable_text`, shared with
`DocumentTextParser`). `CardGenerator`, the transcript screening and the content filter are unchanged:
they only read a source's title and text, which a test pins as for DEC-75. The stored-result
discriminator had to learn the new shape: it told sources apart by a `document_id` key, which a
transcript source has too, so a transcript would have been read back as a malformed document source.

**Precision.** A cited range always contains its speech and never runs past the audio. Its end is
accurate to a few hundred milliseconds. Its start can be early by the length of a pause before it:
across the fixture's 6-second silence, faster-whisper starts the second sentence's segment at 11.4 s
although VAD (correctly) finds speech resuming at 17.4 s, a property of how faster-whisper maps segments
back across the silence it removed. Word timestamps did not change that (11.3 s) and cost 10–16 % more
CPU, so they are off. For "play from here" that errs the right way.

**The contract change** (DEC-75's precedent): `openapi.yaml` and `api-contract.md` gained the variant and
the regenerated `@deckly/api-contract` types carry it. The Zod schema needed more than the new shape: it
was a strict `discriminatedUnion`, so a note with any source kind the app does not know failed the whole
job's parse, although the contract tells clients to ignore unknown kinds. Sources are now filtered to
known kinds before validation, which still rejects a malformed source of a known kind.

### Transcript cache

A second generation from the same recording with other `noteTypes`, `difficulty` or `cardCount` is a
different result-cache fingerprint (see "Redis memory and eviction"), so before DEC-89 it decoded and
transcribed the recording again, which is the expensive part of the job. `RecordingTranscriber` now
checks a transcript cache first, keyed by `document_id` alone, and skips the temporary files, ffmpeg
and the engine process on a hit (`infrastructure/transcription/cache.py`,
`deckly:transcript:v1:<document_id>`, on the same cache Redis as the result cache). What is stored is
the raw transcript (segments with `start_ms`, `end_ms`, text, plus the detected language), not the
passages: grouping and the character budget are applied on every read, so a request with a different
budget is served from the same entry. A hit still downloads the recording, because the pipeline
fetches the bytes before it asks the parser to parse them; skipping that needs a pipeline change.

- **Why `document_id` alone is a safe key.** The pipeline resolves the document for the requesting
  client (`documents.store.get(document_id, client_id)`) before anything is parsed, so a client who
  does not own a document never reaches its transcript. A document's bytes never change after upload.
  What the key does not capture is the engine: the `v1` in the prefix is the model and settings
  version, so changing the pinned model means bumping it.
- **TTL: its own setting, never longer than the document's.** `DECKLY_CACHE_TRANSCRIPT_TTL_SECONDS`
  (default one hour, the shortest document retention) is validated to be at most
  `DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS`, the same shape as the idempotency key TTL against job
  retention. A transcript is the user's speech: it must not be kept for longer than the document it
  came from. It is not simply the retention window because the second request that benefits arrives
  while the user is still working on the deck, and a shorter entry costs Redis memory for less time;
  raise it toward the retention if users come back to a recording later in the day. The TTL runs from
  the write, which is after the upload, so an entry can outlive the document by the time the job
  waited in the queue and transcribed, at most; the job that wrote it references the document for
  the job retention (at least a day), so the document is normally still there.
- **No explicit invalidation when the sweep deletes a document.** The sweep cannot be ahead of the TTL
  in a way that matters: a swept document is no longer found by `documents.store.get`, so the cached
  transcript is unreachable from then on, and it expires on its own within the TTL. An explicit
  delete would make the sweep, which runs inside a Postgres transaction next to object-storage
  deletes, depend on a third system for no behaviour a client can observe.
- **Fails open.** A Redis timeout, an error or a corrupt entry is a miss on read and a logged warning
  on write (`transcript_cache_read_failed`, `transcript_cache_write_failed`); transcription runs. Metric:
  `deckly_transcript_cache_lookups_total{result}` with the result cache's labels.
- **What is not cached.** A transcript of audio cut at the duration cap (`recording_decode_truncated`),
  because the cap is a setting and a later, higher cap would otherwise keep serving the cut transcript
  for the rest of the TTL. A failed transcription caches nothing. A recording with no speech is cached,
  so it is not re-transcribed just to fail with `DOCUMENT_HAS_NO_TEXT` again.
- **Not done: single flight.** Two jobs for the same document that start before either has written
  the entry both transcribe. The transcription worker takes one job at a time per replica, so this
  needs two replicas and two simultaneous requests for the same recording; a lock would cost a
  second Redis key and a wait path for a saving of one transcription.
- **Memory.** An entry is the transcript as JSON, tens of KB for a recording at the duration cap (not
  measured), on the cache instance, which is sized for result entries of 6 to 8 KB ("Redis memory
  and eviction"); it is evicted like them.

### Provider interface and the paid-API swap

`TranscriptionClient` (`client.py`) mirrors `LlmClient`: `transcribe(DecodedAudio) -> Transcript` and
`aclose()`, with `TranscriptionError`, `TranscriptionUnavailableError` (a `TransientError`, so a paid
adapter can sit behind `ResilientCaller` like the LLM clients) and `TranscriptionResponseError`.
`Transcript` is provider-neutral: segments of `start_ms`, `end_ms` and text, and the detected language.
Everything self-hosted (model directory, threads, beam) lives in the adapter's constructor
(`EngineInvocation`), as `LlmEndpoint` does for text models.

The port takes **decoded, capped audio**, not the stored file, on purpose: the truncation guarantee above
then holds for every provider, and a paid one is never sent, or billed for, more than the cap. Checked,
not built, against the three named: OpenAI's transcription endpoint takes WAV up to 25 MB per request (16
kHz mono WAV reaches that at about 13 minutes, so its adapter splits, or encodes to FLAC), and returns
segments with timestamps in `verbose_json`; Deepgram and AssemblyAI take a raw upload and return
utterances or sentences with timestamps. Each is a new adapter that wraps the PCM in a WAV header
(standard library `wave`), maps its segments, and is wired in `build_transcription_client`; the
transcriber, the pipeline and the contract do not change. When one exists, transcription can run in the
generation worker's own process too, since it would be I/O.

### Model weights

`Systran/faster-whisper-small` (MIT, as are OpenAI's Whisper weights it was converted from), revision
`536b0662742c02347bc0e980a01041f333bce120`, 484 MB. `make model` downloads `config.json`, `model.bin`,
`tokenizer.json` and `vocabulary.txt` from that revision over HTTPS and checks each against
`infrastructure/transcription/model.sha256`, re-downloading any that is missing or does not match; the
transcription worker checks the same file at startup (about a second to hash) and refuses to start on a
mismatch, and the engine loads with `local_files_only` and `HF_HUB_OFFLINE=1`, so nothing ever talks to
the Hugging Face Hub at run time. The checksums, not the revision, are the pin: a revision can be
re-pointed, the bytes cannot. The directory (`DECKLY_TRANSCRIPTION_MODEL_DIRECTORY`,
`models/faster-whisper-small`, ignored by git) belongs in the worker image, fetched at build time. CI
caches it keyed on the checksum file.

### New dependencies

Researched to the depth of DEC-8, DEC-9, DEC-84 and DEC-85. They are a dependency group,
`transcription`, which `make install` installs (`default-groups`), so development, type checking and CI
have them; **the API image should be built with `uv sync --no-group transcription`**, and the
import-linter contract "Only the transcription engine, in its own process, loads the speech model" fails
the build if anything but `engine.py` imports `faster_whisper`, `ctranslate2`, `av` or `onnxruntime`. All
are exact pins in `uv.lock`, so `make audit` covers them.

| Package         | Version | License                                                                 | Why it is here                                                                                                                                                      |
| --------------- | ------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| faster-whisper  | 1.2.1   | MIT                                                                     | The engine. Active repository (commits this week) but no release since 2025-10; its PyAV dependency is unbounded (`av>=11`), which is how 19.0 broke `decode_audio` |
| ctranslate2     | 4.8.3   | MIT                                                                     | The inference runtime (released 2026-10), wheels for manylinux x86-64 and aarch64 and macOS                                                                         |
| av (PyAV)       | 19.0.1  | BSD-3-Clause, bundling FFmpeg (LGPL-3.0) and libx264/libx265 (GPL-2.0+) | Required by faster-whisper; never called, see below                                                                                                                 |
| onnxruntime     | 1.31.0  | MIT                                                                     | Silero VAD; 76 MB, pulls `protobuf` (BSD-3) and `flatbuffers` (Apache-2.0)                                                                                          |
| tokenizers      | 0.23.3  | Apache-2.0                                                              | Whisper's tokenizer                                                                                                                                                 |
| huggingface-hub | 2.2.0   | Apache-2.0                                                              | Required by faster-whisper; offline at run time. Pulls `hf-xet`, `fsspec`, `filelock`, `tqdm`                                                                       |
| numpy           | 2.5.4   | BSD-3-Clause                                                            | Already transitive; now imported by the engine                                                                                                                      |

About 170 MB installed. faster-whisper and CTranslate2 ship no type information, which mypy's
`disallow_any_unimported` refuses, so `stubs/faster_whisper/__init__.pyi` declares the three names the
engine uses (`WhisperModel`, `Segment`, `TranscriptionInfo`) with the signatures it calls; the
integration tests exercise the real ones. **ffmpeg moves from a test-only binary to a runtime dependency
of the transcription worker** (Debian `ffmpeg` in its image, as CI already installs).

**PyAV's bundled GPL libraries.** This is not a legal opinion, only the facts that a review needs, in the
same spirit as the yt-dlp and mutagen notes in "Recordings". PyAV's own code is BSD-3-Clause, and the
FFmpeg it bundles reports itself as "LGPL version 3 or later" (`--enable-version3`, without
`--enable-gpl`). The same wheel nonetheless ships `libx264` and `libx265`, which are GPL-2.0-or-later and
which that FFmpeg links for its encoders (`--enable-libx264 --enable-libx265`, seen in the bundled
library's configuration string). It cannot be left out: faster-whisper imports PyAV when it is imported.
The posture taken, like this project's other license decisions:

- **Never invoked.** The service never opens a file with PyAV and never encodes video; the engine hands
  faster-whisper NumPy arrays, and the import contract above keeps PyAV out of every other module.
  Decoding is done by the separate `ffmpeg` program.
- **Not distributed.** The GPL's obligations attach to distributing the binaries. The transcription
  worker's image runs on this project's own infrastructure and is not given to anyone; the mobile app
  contains none of it, and the API image does not install the group at all.
- **What would change it.** Shipping that image to a third party (an on-premises offering, a published
  container) would distribute GPL code and needs a real review first, as would statically linking any of
  it. The Debian `ffmpeg` program is GPL as well, but runs as a separate program rather than being
  linked, and the same review would cover it. The way out, if one is needed, is a PyAV built without
  those encoders, which faster-whisper does not mind.
- mutagen was rejected in DEC-84 because the probe would have **called** GPL code in the API process for
  every upload; this is the different case of GPL code that is installed but never run.

### Running it

```bash
make model                  # once: fetch and verify the pinned model into models/
make transcription-worker   # deckly-transcription-worker, next to make worker
```

Logs: `recording_decoded` or `recording_decode_truncated`, `recording_transcribed` (audio seconds,
transcription seconds, real-time factor, language, segments), `recording_transcription_failed`,
`transcript_parsed`. Metrics, on the transcription worker's own port
(`DECKLY_OBSERVABILITY_TRANSCRIPTION_WORKER_METRICS_PORT`, 9465; the two workers' ports must differ):
`deckly_transcription_real_time_factor` (a histogram: compare it with the 0.6 the deadline allows) and
`deckly_recording_decode_truncations_total`, beside the usual job and stage metrics with `source="recording"`.
`DECKLY_TRANSCRIPTION_ENABLED=false` is the escape hatch for a deployment without the worker: the API
then answers a recording's generation `422 DOCUMENT_NOT_SUPPORTED_FOR_GENERATION` before the quota is
touched, as before DEC-86, and the transcription checks are skipped.

### Fixtures

`tests/fixtures/recordings/speech-en.mp3` (11.5 s), `speech-ru.mp3` (7.2 s) and `speech-pause.mp3` (the
English sentence, 6 s of silence, a later sentence of the same lecture; 22.7 s) are cut from the LibriVox
recordings above, which are public domain, as 24 kbps 16 kHz mono MP3 (126 KB together):

```bash
q=(-hide_banner -loglevel error -y -bitexact)
# en.mp3: archive.org/download/russian_realities_problems_1909_librivox/russianrealitiesandproblems_01_various_64kb.mp3
# ru.mp3: archive.org/download/zapiskiohotnika_2409_librivox/zapiskiohotnika_01_turgenev_64kb.mp3
ffmpeg -hide_banner -loglevel error -y -ss 30 -t 900 -i en.mp3 -c copy en15.mp3   # likewise ru15.mp3
ffmpeg "${q[@]}" -ss 0 -t 11.5 -i en15.mp3 -ac 1 -ar 16000 -c:a libmp3lame -b:a 24k speech-en.mp3
ffmpeg "${q[@]}" -ss 0 -t 7.2 -i ru15.mp3 -ac 1 -ar 16000 -c:a libmp3lame -b:a 24k speech-ru.mp3
ffmpeg "${q[@]}" -ss 0 -t 11.5 -i en15.mp3 -f lavfi -t 6 -i anullsrc=r=16000:cl=mono -ss 18.12 -t 5.2 -i en15.mp3 \
  -filter_complex "[0:a]aresample=16000,aformat=channel_layouts=mono[a];[2:a]aresample=16000,aformat=channel_layouts=mono[b];[a][1:a][b]concat=n=3:v=0:a=1[out]" \
  -map "[out]" -ac 1 -ar 16000 -c:a libmp3lame -b:a 24k speech-pause.mp3
```

The lying file is built in the test from `speech-pause.mp3` (remuxed into MP4 by ffmpeg, then
`understated_timing` in `tests/recording_files.py`). The model-backed tests are integration tests, so
`make test` needs neither the model nor ffmpeg.

### Generating from a recording

DEC-86 already did most of the wiring: `POST /v1/generations` takes a recording's `documentId` exactly as
it takes a document's (the stored record knows the format, so there is no second discriminator),
admission routes it to the transcription queue (`lane_for`), the transcription worker runs the same
`RunGeneration` with `RecordingTranscriber` as its parser, and the full transcript is screened like a
document's text. The admission screen sees the recording's filename (for a fetched video, its title) and
the instructions, like a document's. Retention needed nothing: the `ON DELETE RESTRICT` key and
`is_usable`/`is_expired` never look at the format. DEC-87 closed what DEC-86 left:

**Source kind.** `SourceKind` is `topic`, `document` or `recording`, decided by
`GenerationRequest.source_kind(document)` from the stored record, so logs, metrics and spans tell a
recording from a document. Admission already holds the record. The worker reads it once, right after the
job is marked running and before the cache is consulted (`RunGeneration._resolve`), and reuses it for
the download, so a job reads its document record exactly as often as before. Reading it inside the
run's failure handling means a store outage fails the job like any other step instead of leaving it
queued. A record that is gone fails the job `DOCUMENT_NOT_FOUND` in `planning`, before the cache: the
foreign key and the retention rule make that unreachable for a stored job. A request naming a document
that admission cannot find is counted as `document`, since there is nothing to tell what it was.

**Cache.** `RecordingGenerationFingerprint` (the same fields as a document's) keys recordings under
`deckly:generation-result:recording:v1`. The keys could not collide before (a document id is unique
across both kinds), but their own namespace lets a change of transcription engine drop every
recording-sourced result by bumping the version without touching documents. `ResultCache` now takes a
`Fingerprint` rather than a request, since only the run knows the document a fingerprint needs.

**Regeneration** of a note from a recording-sourced deck is refused with
`422 DOCUMENT_REGENERATION_UNSUPPORTED`, through DEC-83's check unchanged: it refuses any `documentId`
before anything is looked up. An integration test uploads a recording and proves the refusal, and that
no regeneration budget is spent.

**Queued recordings go stale on their own clock.** The transcription worker takes one job at a time,
and a job may use its whole `DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS` (960 s). Under the shared one-hour
queued threshold, the fifth of five recordings queued together would start after 4 x 960 = 3,840 s and
be failed `GENERATION_FAILED` while still waiting its turn. A test replays exactly that.
`StalenessPolicy` now has `recording_queued_after`; the sweep lists stale jobs per queue (`in_lane`
joins a job to its document's format), and running jobs keep the one shared threshold.

`DECKLY_SWEEP_RECORDING_QUEUED_STALE_AFTER_SECONDS` defaults to 10,800 s (three hours). It is sized
against the backlog the queue-depth alert above allows: 8 waiting plus 1 running per transcription
worker, each taking up to the job timeout, one at a time, is 9 x 960 = 8,640 s before the last one
starts. Startup refuses a threshold that does not outlast that plus the job store timeout, and moves
with the job timeout (the default fits timeouts up to 1,199 s). The 2,160 s of headroom is about two more
worst-case jobs, time to act on the alert. With typical jobs (a 15-minute recording takes 3 to 5
minutes end to end) three hours absorbs a backlog of roughly 35 to 60. Replicas do not change the check: the alert is per replica,
and so is the drain rate. A recording also cannot go stale sooner than other queued jobs
(`SweepSettings` checks that). The cost is the other side of the same threshold: a recording job whose
enqueue was lost shows as queued for up to three hours, not one, before the sweep fails it.

### YouTube captions instead of transcription: not pursued (DEC-90)

**Decision: a fetched video is always transcribed, even when YouTube has captions for it.** Captions are
no more accurate than this engine, the saving they offer is CPU nobody is short of yet, and they would
optimise a path that may not run in production at all. The measurements below were taken on
2026-10-19 with yt-dlp 2026.8.19 from a developer's residential address, so they should not be
re-taken without a reason that changes the decision (below).

**Where captions would come from.** DEC-85's probe JSON already lists them: creator tracks under
`subtitles` and YouTube's speech recognition under `automatic_captions`, where the track in the spoken
language is keyed `<lang>-orig` (`VideoInfo` ignores both). The files are served from
`www.youtube.com/api/timedtext`, which the egress proxy already admits, and downloading them inside
the probe cost no second extraction (9.8 s against about 10 s for a plain probe, which is mostly the
player challenge). No PO token was needed. Three things the metadata does not make simple: a video
with dubbed audio tracks has one `-orig` track per track (66 of 526 sampled), so the track has to match
the chosen audio format's `language`; creator tracks can be named (`en-ehkg1hFWq8A`) and past streams
list `live_chat` among them; and when YouTube's subtitle PO-token experiment marks a track
(`exp=xpe` or `xpv` in its URL), yt-dlp drops it with only a debug line, so from an address in the
experiment "no captions" can mean "captions withheld".

**Availability.** 526 videos, the results of 40 searches for educational content (lectures, lessons,
tutorials; 20 in English, 20 in Russian), probed with the production flags and no download:

| Up to the 15-minute cap | Videos | Speech recognition only | Creator track in the spoken language | Creator track, no speech recognition | Nothing |
| ----------------------- | ------ | ----------------------- | ------------------------------------ | ------------------------------------ | ------- |
| English searches        | 101    | 47.5 %                  | 44.6 %                               | 5.9 %                                | 2.0 %   |
| Russian searches        | 58     | 87.9 %                  | 8.6 %                                | 0 %                                  | 3.4 %   |

A creator track is not necessarily a human transcript. Every one in the table was downloaded: 42 of the
45 English ones are punctuated and cased, and one is lowercase without punctuation. Of the five
Russian ones, four are the speech-recognition track re-uploaded word for word and one could not be told
apart. A Khan Academy video outside the sample was a re-upload too. So a human transcript exists for
about 45 % of short English videos and almost no Russian ones, and only the text, not the metadata,
says which.

**Quality.** Thirteen videos from 5 to 13.5 minutes, with the speech-recognition track and the shipped engine
(`small`, int8, beam 1, VAD) run on the same audio, scored against the human creator track where there
is one. Fillers, digits against number words and punctuation are normalised away before scoring;
_term recall_ is the share of the reference's proper nouns and words of nine letters or more that each
transcript contains, the words a card is most likely to be about.

| Scored against a human transcript                       | Speech recognition, WER | This engine, WER      | Term recall (recognition / engine) |
| ------------------------------------------------------- | ----------------------- | --------------------- | ---------------------------------- |
| English, 6 videos (TED, TED-Ed, Crash Course)           | 2.7–8.3 %, mean 5.7 %   | 2.1–7.7 %, mean 4.8 % | 93.1 % / 93.6 %                    |
| Russian, a lecture                                      | 9.4 %                   | 10.1 %                | 92.2 % / 92.2 %                    |
| Russian, a studio talk show with music and two speakers | 33.9 %                  | 26.3 %                | 75.9 % / 75.0 %                    |

The engine was better on four of the six English videos. On four Russian lessons with no human track
the two disagree on 7.4–17.5 % of words, and reading the disagreements, each is right about as often as
the other. The errors differ in kind more than in number:

- **Speech recognition** has no punctuation or casing (12 of 13 tracks; YouTube punctuated one), keeps
  fillers, and repeats its mistakes on the subject's own term ("Duolingo" as "dualingo" six times in one
  talk). In Russian it switches to Latin letters (`react`, `ekaterin`) and runs words together.
- **This engine** punctuates, but drops whole phrases now and then ("that's how you make more money")
  and, in Russian, garbles word roots ("вычет" as "вычатку", "ипотека" as "эпотека"). It hallucinated
  nothing: no repeated phrases, and no text over music intros, which VAD removes. Its first segment
  after an intro starts early (the property described under "Transcripts and citations"), where creator
  cues were accurate.

So only a human transcript is better, and only for English, by about five points of word error rate and
six of term recall.

**Timestamps.** All three fit `TranscriptSegment(start_ms, end_ms, text)`; no shape change is needed.
Run through `passages` and `TranscriptSource`, every source was valid. Creator cues (median 2.3 s) do not
overlap and produce the same passages as the engine. Speech-recognition events in `json3` are a rolling
two-line display: each overlaps the next, so passages would overlap by up to 5.6 s, and the last can end
1.8 s after the audio. Ends would have to be clamped to the next start and to the duration; 97 % of
events carry word offsets (`tOffsetMs`) to cut on.

**Why not.** What captions would save is transcription: at the measured real-time factor (0.10–0.14 on
two native cores for these videos) 1.5 to 2 minutes of two cores, an estimated 4 to 5 on cloud vCPUs, and
about 1 GiB of memory per 15-minute recording, for the 97 % of videos that have some track. That is worth having only when transcription is
a bottleneck, and nothing says it is: there is no production traffic, and since DEC-89 a second
generation from the same recording does not transcribe again. Against it, it would need track selection
(the three cases above), a `json3` parser, captions stored with the document (a migration), captioned
jobs routed off the transcription queue, and its own fallbacks and tests, on top of a fetch that YouTube
may refuse from a server's address altogether ("Production caveat"). A creator-tracks-only variant would
be the same plumbing for a quality gain that exists in English only.

**Reopen** when recordings from a URL are fetched successfully from the production address (the
`blocked` outcome stays low) **and** transcription is measurably the constraint: the transcription
queue-depth alert firing, or `deckly_transcription_real_time_factor` showing jobs near the deadline.
A user-visible quality problem with English transcripts would be a second reason; captions would not
help Russian. The design then: choose the track from the probe's JSON (a punctuated creator track in
the audio format's language, else that language's `-orig` track, else none), download it in the same
call as the audio with `--write-subs --write-auto-subs --sub-langs <track> --sub-format json3`, store the
parsed segments with the document, use them in place of the engine, and fall back to transcription
when there is no track or its text is too thin for the duration.

**Two constraints learned on the way.** `--write-auto-subs` makes yt-dlp list every creator track
machine-translated into every language: 24 tracks became 3,904 entries and a 12 MB info JSON, so a
video with about 33 translations would pass the 16 MiB metadata cap and fail the fetch. Passing
`--extractor-args youtube:skip=translated_subs` brought the same video to 697 KB. Today's probe passes
neither flag and is not affected. Separately, **a bug in today's fetch**: when YouTube rate-limits,
yt-dlp prints the `429` as a warning and ends with `ERROR: … This video is not available`, which
`classify` reads as `RECORDING_UNAVAILABLE` (`422`, outcome `unavailable`) instead of `blocked`, so the
block is reported to the user as a removed video and does not reach the series to alert on. Seen live
during this sampling (about 250 probes in a few minutes, four at a time, drew the bot check from a
residential address; one at a time every ten seconds, the next 300 did not). Not fixed here.

### Not done here

- **DEC-89:** caching a transcript per document, so a second generation from the same recording does not
  transcribe it again.
- **Frontend:** the app does not upload recordings or generate from them yet; its contract types and Zod
  schema know the transcript source (above), its UI does not.
- **Progress within `parsing_sources`** (DEC-91), and a running-job sweep that knows a transcription job's
  timeout from a topic job's (the queued-job sweep does since DEC-87).
- **A benchmark on the deployment's hardware.** The cloud speed above is an assumption; the
  real-time-factor histogram is how to replace it.

## Production deployment

Nothing in this repository deploys the service yet; these are the decisions a deployment has to
honour. Local `docker-compose.yml` is not a production configuration.

### Redis memory and eviction

One Redis holds three kinds of data with different loss tolerance: the Arq queue (jobs and abort
markers), the quota counters (`deckly:generation-quota:*`) and the generation result cache
(`deckly:generation-result:*`, only the worker reads and writes it). Losing a cache entry costs one
regeneration. Losing a queued job or a counter loses a job or a budget silently.

**Decision: the result cache gets its own Redis instance in production.**

| Instance     | Holds                    | `maxmemory`                     | `maxmemory-policy` | When full                               |
| ------------ | ------------------------ | ------------------------------- | ------------------ | --------------------------------------- |
| queue, quota | Arq keys, quota counters | explicit, sized for the backlog | `noeviction`       | writes fail loudly: `503`, alertable    |
| cache        | `generation-result:*`    | explicit, its own budget        | `allkeys-lru`      | oldest entries go; a miss costs one run |

Alternatives that were rejected:

- **A separate database on the same instance.** `maxmemory` and the eviction policy belong to the
  instance, not the database number, so this isolates nothing.
- **One instance with `volatile-lru`.** Arq's job keys, abort markers and the quota counters all
  carry a TTL, which is what `volatile-*` evicts. Cache pressure would drop queued jobs and
  counters without an error. `allkeys-lru` is worse.
- **One instance with `noeviction`.** A full cache then makes the enqueue and the quota
  reservation fail with `503 UPSTREAM_UNAVAILABLE`, which turns an optimisation into an outage.
  The cache cannot be bounded from inside the application: its size is the number of distinct
  requests inside `DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS`, which a client controls.

Sizing the queue and quota instance: a quota counter is about 120 bytes and every admitted job
writes two, expiring after a day, so it grows with jobs per day, not with the cache. Measure the
job keys on the target (`MEMORY USAGE arq:job:<id>`), multiply by the longest backlog the queue
should hold, add the counters, and double it. Cache entries are 6 to 8 KB in a local run with a
few cards each and grow with deck size, so size the cache instance from the number of distinct
topics worth keeping for the TTL. Alert on `used_memory` against `maxmemory` on both.

The cache already fails open: a timeout, an out-of-memory reply or a corrupt entry is a miss
(`infrastructure/result_cache.py`), so the cache instance needs no special handling when full.

`DECKLY_CACHE_REDIS_URL` points the cache at its own instance. It is optional and only the worker
reads it (`build_cache_redis` in `worker/settings.py`). Unset, the cache uses `DECKLY_REDIS_URL`,
so a single local Redis keeps working, and a production deployment must set it: on one instance
with a memory limit the cache competes with the queue and the quota for memory. A blank value is
rejected instead of read as unset, so a templated deployment cannot fall back to the shared
instance without saying so. Local `docker-compose.yml` runs the shared instance with
`--maxmemory 256mb --maxmemory-policy noeviction`, which is the queue and quota row, so a full
Redis fails the way it would in production.

The cache connection is built like the queue's (`create_bounded_pool` in `infrastructure/queue.py`):
the same connect and read timeout from `DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS`, so a cache instance
that accepts connections and then freezes fails a command after that timeout instead of hanging
the job. Unlike the queue pool it is not pinged at startup. The worker boots with the cache
instance down or unreachable and treats every lookup as a miss, because the cache must not be able
to stop generation. A URL whose database path is not a number (`redis://host/abc`) is not
reachability: it passes config validation, as it does for `DECKLY_REDIS_URL`, and stops the worker
at startup. The symptom is `generation_cache_read_failed` warnings and
`deckly_generation_cache_lookups_total{result="error"}`, so alert on it. The integration test points the
cache at another database of the local Redis rather than a second container: instance and
database number go through the same URL parsing and connection parameters, which is the code
under test, while memory and eviction separation is a property of the Redis deployment.

### Object storage

- **Private bucket.** No public access and no public bucket URL: documents are read only through the
  API. On R2 leave the `r2.dev` subdomain and custom domains off; on AWS keep Block Public Access on.
- **Credentials scoped to the one bucket.** An R2 API token with Object Read & Write on that bucket
  only, or an IAM user whose policy allows `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on
  `arn:aws:s3:::<bucket>/documents/*` and nothing else. The worker reads every document it
  generates from, so `GetObject` is required.
- **No lifecycle rule is needed.** Every object has a row from before it was written, and the sweep
  deletes the object before the row, so nothing escapes it. A rule that expires objects under
  `documents/` cannot be a second line: a document a client keeps generating from has no fixed
  lifetime (see "Generating from a document"), and a rule that deletes its object fails the next
  job with `DOCUMENT_NOT_FOUND`.
- **Deny internal ranges to the API's egress.** The recording fetch's proxy already refuses them
  (see "Recordings from a URL"); a security group or firewall rule that blocks RFC 1918, link-local
  (including `169.254.169.254`) and the cloud's metadata endpoints is the layer under it.
- **The reverse proxy must allow the fetch's response time** on `/v1/recordings/from-url`, at least
  `DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS` plus the upload to the object store.
- **The reverse proxy must allow the body.** At least `DECKLY_LIMIT_DOCUMENT_MAX_BYTES` plus 16 KiB
  on `/v1/documents` (nginx's default `client_max_body_size` is 1 MiB), and a request-body read
  timeout: uvicorn does not bound how long a client may take to send its body, so a slow sender
  holds a connection until the proxy cuts it.

### Metrics access

`/metrics` exposes throughput, provider latency and error rates and the queue depth, with no
authentication. It must not be reachable from the internet.

**Decision: network placement, not endpoint authentication.**

- **API.** `GET /metrics` shares the API's port. The TLS-terminating reverse proxy that fronts the
  API answers `/metrics` with `404` and forwards only `/v1/` (and nothing else the service does not
  need to publish). uvicorn listens on a loopback or private address, never on a public
  interface, and Prometheus scrapes uvicorn directly on that private address, not through the
  proxy. The proxy rule is the second line; the listen address is the first.
- **Worker.** It has no public API, and `DECKLY_OBSERVABILITY_WORKER_METRICS_HOST` is
  `127.0.0.1` by default, so a scraper on the same host reaches it and nothing else does. When
  Prometheus runs elsewhere, set it to the host's private address and let only the scraper reach
  port `9464` (security group or firewall). `0.0.0.0` on a host with a public interface exposes
  it. The `observability` compose profile needs `0.0.0.0` on Linux, which is a local-only setting.

Alternatives that were rejected: a second listener for the API's metrics (another HTTP server in
the API process, and it still needs the same network rule), and authenticating the endpoint
(Prometheus then needs a secret distributed to it, and an unauthenticated internet-facing
`/metrics` still gets probed). Check a deployment with `curl -i https://<public host>/metrics`
(expect `404`) and `curl http://<public address>:9464/metrics` (expect a refused connection).

### Transcription worker

Its own container, with its own CPU quota and a memory limit that leaves room for
`DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES`; Debian's `ffmpeg` installed; the model fetched by
`make model` at image build time (not at start, so a replica never depends on the Hugging Face Hub);
a scratch directory on disk rather than a tmpfs; `DECKLY_TRANSCRIPTION_CPU_THREADS` set to the
container's vCPUs. The API image is built without the `transcription` dependency group. Alert on the
transcription queue's depth and watch the real-time factor histogram (see "Transcription").

## Checks

```bash
make check          # suppression guard, ruff format --check, ruff check (incl. complexity budget),
                    # mypy --strict, import-linter, vulture
make fix            # ruff format + ruff check --fix
make test           # pytest with coverage, no infrastructure needed
make test-integration  # Postgres + Redis tests, appends to the coverage data; needs make up && make migrate,
                       # and ffmpeg and make model for the transcription tests
make coverage-gate  # coverage floors; run after make test and make test-integration
make audit          # pip-audit over uv.lock; needs network
make test-live     # calls the model provider configured in .env; needs a real key, costs money
```

CI runs `make install`, `make check`, `make test`, then `make migrate`, `make test-integration` and
`make coverage-gate` against Postgres and Redis service containers (`.github/workflows/ci.yml`,
`backend` job). `make audit` runs in the `backend-audit` job and a gitleaks scan of the full git
history runs in the `secrets` job. The numbers and the reasoning are in
[`.claude/backend/engineering-guide.md`](../.claude/backend/engineering-guide.md) → "CI enforcement".

### Integration tests leave no rows behind

`make test-integration` writes to whatever `DECKLY_DATABASE_URL` and `DECKLY_REDIS_URL` name, which
is the compose stack from `make up` unless you override them. Every test removes the
`generation_jobs` rows it created in its teardown (`Cleanup` in `tests/integration/conftest.py`;
the document tests remove their `documents` rows, stored objects and upload counters the same way),
so a completed run, passing or failing, leaves the table as it found it. A run that is killed
before teardown (`kill -9`, a closed terminal, a CI timeout) does not, and its rows stay.

To prove a change keeps that property, run the suite against a scratch stack on other ports
instead of the development one:

```bash
docker run -d --name deckly-scratch-pg -e POSTGRES_USER=deckly -e POSTGRES_PASSWORD=deckly \
  -e POSTGRES_DB=deckly -p 5543:5432 postgres:17-alpine
docker run -d --name deckly-scratch-redis -p 6390:6379 redis:7-alpine
export DECKLY_DATABASE_URL=postgresql+asyncpg://deckly:deckly@localhost:5543/deckly
export DECKLY_REDIS_URL=redis://localhost:6390/0
make migrate
make test-integration && make test-integration
docker exec deckly-scratch-pg psql -U deckly -d deckly -tAc 'select count(*) from generation_jobs'
docker rm -f deckly-scratch-pg deckly-scratch-redis
```

The count must be `0` after both runs. Against the development database the same query only
makes sense as a before/after pair, because jobs created through a running API are rows too.
