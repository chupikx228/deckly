# Backend testing guide

How to test the generation backend. Stack-agnostic, like
[`engineering-guide.md`](engineering-guide.md) — it prescribes _what_ to test and _what not
to_, not a test runner.

## The one principle: test the boundaries, not the happy path

The happy path is the least likely place a bug hides and the most likely thing to be exercised
by hand during development anyway. A suite that is mostly happy-path assertions is green and
useless — it gives confidence while the edges are broken.

So write **one** happy-path case per behaviour, as an anchor that documents intent, and spend
the rest of the budget where bugs actually live:

- **boundaries** — min, max, one below min, one above max, empty, zero, off-by-one;
- **failure modes** — what the model, the search provider and the network do _wrong_;
- **invariants** — the things that must never be true, regardless of input;
- **state transitions** — the illegal ones, not just the legal ones;
- **idempotency and concurrency** — the same request twice, cancellation mid-flight.

If a test would still pass when you delete a random branch of the code, it is testing the
happy path. Delete it or point it at a boundary.

## What each layer tests

Mirrors the layers in [`engineering-guide.md`](engineering-guide.md). Dependencies point
inward, so tests get cheaper and more valuable the further in you go.

| Layer           | Kind                        | Runs                             | Focus                                                                    |
| --------------- | --------------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| **Domain**      | Pure unit, no I/O           | Every commit, milliseconds       | Invariants and the job state machine. The cheapest, most valuable tests. |
| **Application** | Unit against fake ports     | Every commit                     | Orchestration under success **and** every failure mode. No network.      |
| **Adapters**    | Integration, real providers | Separately, rarely (cost, flaky) | The seam with the real model/search/store. Not in the fast suite.        |
| **Transport**   | Contract tests              | Every commit                     | Every response validates against `openapi.yaml`, every error `code`.     |

The bulk of the suite is domain + application. Adapters get a thin integration layer, not
exhaustive coverage — you are not testing the provider's SDK.

## The boundaries that matter for this backend

This is the checklist. These are the cases worth writing; the happy path is assumed.

### Request validation

- `topic` at length 2 (reject), 3 (accept), 200 (accept), 201 (reject).
- `cardCount` at 4 (reject), 5 (accept), 200 (accept), 201 (reject).
- `noteTypes` empty array (reject), unknown enum value (reject), duplicates.
- Unknown/extra fields rejected (`additionalProperties: false`).
- Missing required headers (`Idempotency-Key`, `X-Client-Id`).
- Bad input yields `400 VALIDATION_FAILED`, never a 500.

### Model output — the untrusted-input suite

This is the heart of an AI backend and where most real bugs will be. The model does **not**
reliably return the wire format; the backend must. Test that it enforces this:

- Malformed JSON, or valid JSON wrapped in prose — the backend recovers or fails cleanly, it
  does not 500.
- `fields` that do not match their `noteType` — dropped, not returned.
- Cloze text with no `{{cN::}}` marker, or markers numbered with a gap (`c1`, `c3`) — repaired
  or dropped.
- Multiple-choice `distractors` that contain the correct answer, or fewer than two — dropped.
- A note with missing or empty `sources` — that note is dropped, not the whole job; the job
  still succeeds with the remaining notes. Every returned note carries at least one source,
  even though the model writes the cards itself (sources are the anti-hallucination guard).
- Media with no `license`, or a URL that 404s — not returned.
- A model returning 40 notes when 50 were asked — accepted; `cardCount` is a target. Assert
  the response is _valid and smaller_, not that it equals the request.
- **Never assert exact generated text.** It is non-deterministic. Assert shape, validity and
  invariants — never `expect(note.fields.front).toBe("...")`.

### Job lifecycle

- Poll an unknown `jobId` → `404 JOB_NOT_FOUND`.
- Cancel a `queued` job, a `running` job (accepted), a terminal job → `409 JOB_ALREADY_TERMINAL`.
- **`progress` never decreases** across a sequence of polls — assert monotonicity explicitly.
- A generation failure surfaces as `200` with `status: "failed"` and a populated `error`, **not**
  as a 5xx. This is the single most important lifecycle test.
- Cancellation actually stops provider work, not just flips a flag.

### Idempotency and concurrency

- Same `Idempotency-Key` twice → the **same** `jobId`, one job, not two.
- Different key → a new job.
- The key is scoped per `X-Client-Id` — two clients with the same key do not collide.

### Resilience

- Provider timeout → `503 UPSTREAM_UNAVAILABLE` with `retryAfterSeconds`.
- Retried only on transient failures — a policy rejection or validation error is **not**
  retried.
- Image provider down but `includeImages: true` → degrades to a deck without images, the job
  does not fail.

### Rate limiting and caching

- Over the per-client limit → `429 RATE_LIMITED` with `retryAfterSeconds`.
- Identical normalised `(topic, language, cardCount, difficulty, noteTypes)` → cache hit;
  reordering `noteTypes` still hits the same entry (normalisation is tested).

## What not to test

- **Not the happy path, five ways.** One anchor case is enough; the rest is noise that slows
  the suite and hides the meaningful tests.
- **Not the framework, the DB driver or the model SDK.** Assume they work. Test _your_ code at
  the seam with them, through a fake port.
- **Not exact model output.** Non-deterministic. Shape and invariants only.
- **Not private helpers directly.** Test the public behaviour; if a helper needs its own test,
  it probably wants to be its own unit with its own interface.
- **No snapshot tests of generated content.** A snapshot of LLM output tests nothing and breaks
  on every model change.
- **Nothing that passes regardless.** A test with no meaningful assertion, or that mocks the
  very thing under test, is worse than no test — it is a green light wired to nothing.

## Determinism

Unit and application tests must be deterministic. Inject, never reach for:

- the clock (`now` is a parameter, tests pass a fixed value);
- randomness and id generation;
- the model, search and image providers (fake ports);
- the job store (in-memory fake).

A test that calls a real LLM is not a unit test — it is a flaky integration test that costs
money. Keep those in the separate adapter suite.

## Coverage

Coverage is a floor, not a goal (see the CI ruleset in
[`engineering-guide.md`](engineering-guide.md)). 95% coverage of the happy path is worthless;
what matters is **branch** coverage of the error paths above. Hold the domain and application
layers higher than the adapters, and never chase 100% by testing getters.

## A good test for this backend

1. Names the boundary or failure it protects, not "it works".
2. Has one clear reason to fail, and fails for that reason.
3. Is deterministic — same result every run, no real network, no wall clock.
4. Asserts shape and invariants for generated content, never exact text.
5. Would go red if you broke the behaviour it describes. If it would not, it is not a test.
