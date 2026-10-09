# Backend API contract

The contract the Python backend must implement. It is written before the backend exists so
the mobile app can be developed against mocks and the two sides meet without an integration
week.

`openapi.yaml` in this folder is the machine-readable source of truth. TypeScript types are
generated from it into `frontend/packages/api-contract` and are never hand-written.

## Scope

The app is offline-first. **Local SQLite owns all user data.** The backend exists for one
purpose: turning a topic into a set of reviewable notes.

Therefore in v1 there are:

- **No** deck, note or card CRUD endpoints
- **No** review or scheduling endpoints — scheduling runs entirely on the device
- **No** auth endpoints — there is no account yet
- **No** sync endpoints — that is phase 5

Adding any of these silently would break the offline guarantee. If the backend needs one,
raise it as a contract change first.

## Conventions

| Concern         | Rule                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| Base path       | `/v1`                                                                                                  |
| Format          | JSON, UTF-8                                                                                            |
| Casing          | `camelCase` in JSON bodies, so no transformation layer is needed on the client                         |
| Timestamps      | ISO 8601 with an explicit offset (`2026-08-14T10:30:00Z`)                                              |
| Ids             | UUID v4 strings                                                                                        |
| Errors          | RFC 9457 `application/problem+json`                                                                    |
| Idempotency     | `POST /generations` requires an `Idempotency-Key` header                                               |
| Client identity | `X-Client-Id` header carrying the anonymous device id, for rate limiting                               |
| Header UUIDs    | Version 4, canonical form only: lowercase, hyphenated (`2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a`)         |
| Locale          | `Accept-Language` header, used for error messages only — card language is explicit in the request body |

## Why generation is asynchronous

Producing 50 cards involves planning the topic, retrieving sources, parsing them, generating
content and sourcing images. That is 30–120 seconds. It does not fit in one HTTP request:
mobile networks drop, the OS suspends backgrounded apps, and proxies time out well before
that.

So `POST /generations` returns immediately with a job id, and the client polls. The stages
are reported explicitly because a two-minute spinner with no information reads as a hang.

## Endpoints

### `POST /v1/generations`

Starts a generation job. Returns `202 Accepted`.

Request:

```json
{
  "topic": "Road signs of the Russian traffic code",
  "language": "ru",
  "cardCount": 40,
  "difficulty": "intermediate",
  "noteTypes": ["basic", "cloze", "multiple_choice"],
  "includeImages": true,
  "instructions": "Focus on warning and prohibitory signs"
}
```

| Field           | Required | Notes                                                                   |
| --------------- | -------- | ----------------------------------------------------------------------- |
| `topic`         | yes      | 3–200 characters after trimming, at least 3 of them visible (see below) |
| `language`      | yes      | BCP 47 tag. The language of the **cards**, not the interface.           |
| `cardCount`     | yes      | 5–200. A target, not a guarantee; the response may return fewer.        |
| `difficulty`    | no       | `beginner` \| `intermediate` \| `advanced`. Defaults to `intermediate`. |
| `noteTypes`     | no       | Which types the generator may produce. Defaults to `["basic"]`.         |
| `includeImages` | no       | Defaults to `false`. Images add significant latency and cost.           |
| `instructions`  | no       | Free-form user steering, up to 500 characters.                          |

**`image_occlusion` needs `includeImages: true`.** An image-occlusion note is built on an image
the server fetches, so a request that asks for that type without images could never produce
one. A request whose `noteTypes` contains `image_occlusion` while `includeImages` is `false` —
or left out, since it defaults to `false` — is rejected up front with `400 VALIDATION_FAILED`.
The schema expresses this rule with `if`/`then` on `GenerationRequest`.

The server also enforces these rules, which the schema cannot express. Each one fails with
`400 VALIDATION_FAILED`:

- **`topic` is trimmed before its length is checked.** Leading and trailing whitespace is
  removed, and the result must be 3–200 characters. A blank or whitespace-only topic such as
  `"   "` is rejected. The trimmed value is what the server stores and generates from.
- **`topic` must contain at least 3 visible characters.** Only characters that render count
  toward the minimum. These do not: whitespace, control characters, format characters
  (zero-width space, BOM, zero-width joiner, bidi marks), combining marks, and characters that
  render blank — the Hangul fillers (U+115F, U+1160, U+3164, U+FFA0), the blank Braille
  pattern (U+2800), U+1D159 and the reserved default-ignorable code points. A combining mark
  counts with the letter it attaches to, and a space between words does not count. So
  `"a\u200Bb"` and `"a b"` are rejected even though each is 3 characters long, and a topic
  made only of such characters is rejected whatever its length. This is the same definition
  of blank the server applies to generated deck titles and note fields. The 200-character
  maximum still counts every character.
- **`cardCount` must be a plain JSON integer literal.** `40` is accepted. `1e2`, `4e1` and
  `40.0` are rejected even though they are whole numbers.
- **`language` must be a well-formed BCP 47 tag.** The server checks the syntax from RFC 5646,
  including the irregular grandfathered tags such as `i-klingon`. The check ignores case and
  accepts ASCII only. It does not check the tag against the subtag registry, so `xx-YY` passes
  and `en_US`, `english` and `en-` do not.
- **`topic` and `instructions` must not contain NUL (`\u0000`).**
- **Every entry in `noteTypes` must be a type the server can generate.** The enum lists every
  type the contract knows about. A type the generator does not support yet is rejected, even
  though it is in the enum. Every type in the enum is currently supported.

Headers:

| Header            | Required | Notes                                                                         |
| ----------------- | -------- | ----------------------------------------------------------------------------- |
| `Idempotency-Key` | yes      | Client-generated UUID in canonical form. See "Idempotency" below.             |
| `X-Client-Id`     | yes      | Anonymous device id in canonical form. Scopes idempotency keys and the quota. |

Both headers must be version 4 UUIDs in canonical form: lowercase hex, hyphenated 8-4-4-4-12,
version nibble `4` and RFC 9562 variant (`8`, `9`, `a` or `b` as the first digit of the fourth
group). The server rejects anything else with `400 VALIDATION_FAILED` and does not normalise
it. That includes uppercase, `{…}` braces, the `urn:uuid:` prefix, the unhyphenated
32-character form, the nil UUID `00000000-0000-0000-0000-000000000000` and UUIDs of any other
version.

**Idempotency.** Keys are scoped per `X-Client-Id`, so two clients can use the same key
without colliding.

- Replaying the same request with the same key returns the original job instead of starting
  a second one.
- Reusing a key with a different request returns `409 IDEMPOTENCY_KEY_CONFLICT` and leaves
  the original job untouched. Replaying the original request with that key still works.
- A key is honoured for 24 hours after the original request
  (`DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS`). After that the same key starts a new job, as if
  it had never been used. The original job is not affected and can still be polled until it is
  deleted (see "Non-functional requirements"). Expired keys are released by a scheduled task,
  so a key can keep replaying for up to one sweep interval past the 24 hours.

Requests are compared after defaults are applied and `topic` is trimmed. Key order, defaults
sent explicitly, and whitespace around the topic therefore do not count as differences.
Everything else is compared exactly, including the case of `language` and the order of
`noteTypes`. A client retrying a request should resend exactly the same body.

Response `202`:

```json
{
  "jobId": "…",
  "status": "queued",
  "createdAt": "2026-08-14T10:30:00Z",
  "quota": { "limit": 20, "remaining": 17, "resetsAt": "2026-08-15T00:00:00Z" }
}
```

`quota` is optional; when present it reports this client's generation budget after the job was
accepted. See "Quota" below. A replay of an earlier request with the same `Idempotency-Key`
returns the original job and the current budget without using up another job.

### `GET /v1/generations/{jobId}`

Polled by the client. Returns `200` in every non-terminal and terminal state; a job that
does not exist, or that has been deleted after its retention period, returns `404`.

**Access.** `X-Client-Id` is required and validated here under the same rules as on
`POST /generations`, but access to a job is not scoped by client: anyone who knows a `jobId`
can poll it. This is intentional. The random v4 job id is itself the capability, because
`X-Client-Id` is an anonymous, spoofable device id and not a security boundary.

```json
{
  "jobId": "…",
  "status": "running",
  "stage": "generating_cards",
  "progress": 0.62,
  "createdAt": "2026-08-14T10:30:00Z",
  "updatedAt": "2026-08-14T10:30:44Z",
  "result": null,
  "error": null
}
```

**Statuses.** `queued`, `running`, `succeeded`, `failed`, `cancelled`. The last three are
terminal; the client stops polling on them.

A job never stays non-terminal forever. If a job stops making progress (its worker died, or it
was never picked up), a scheduled sweep fails it with `GENERATION_FAILED`: a `running` job
after 15 minutes without a state change, a `queued` job after an hour.

**Stages**, reported in order so the client can show meaningful progress:

| Stage                | Shown as                               |
| -------------------- | -------------------------------------- |
| `planning`           | Working out the structure of the topic |
| `retrieving_sources` | Searching for sources                  |
| `parsing_sources`    | Reading the material                   |
| `generating_cards`   | Writing the cards                      |
| `fetching_media`     | Finding images                         |
| `finalizing`         | Finishing up                           |

`progress` is `0.0`–`1.0` and is monotonic — it must never decrease.

The response should include `Retry-After` in seconds while the job is non-terminal. The
client polls every 2 seconds by default and gives up after 5 minutes.

**Result**, present only when `status` is `succeeded`:

```json
{
  "deck": {
    "title": "Road signs",
    "description": "Warning and prohibitory signs of the Russian traffic code",
    "tags": ["driving", "signs"]
  },
  "notes": [
    {
      "clientId": "…",
      "noteType": "basic",
      "fields": { "front": "What does a red triangle sign mean?", "back": "A warning sign" },
      "media": [],
      "sources": [
        {
          "title": "Traffic regulations",
          "url": "https://…",
          "retrievedAt": "2026-08-14T10:30:20Z"
        }
      ]
    }
  ]
}
```

`deck.title` is at most 120 and `deck.description` at most 500 characters, counted in **UTF-16
code units**, not Unicode code points. That is what JavaScript's `String.length` returns, so the
client's own check agrees with the server's. A character outside the Basic Multilingual Plane,
such as most emoji, counts as 2: a title of 60 emoji is at the limit, and 61 is over it.

### `POST /v1/generations/{jobId}/cancel`

Cancels a running job. Returns `204`. Cancelling an already-terminal job returns `409`.

**Access.** `X-Client-Id` is required and validated here under the same rules as on
`POST /generations`, but as with `GET /generations/{jobId}`, access to a job — including
cancelling it — is not scoped by client: anyone who knows a `jobId` can cancel it. Cancel is
more destructive than a read, and this is still accepted deliberately, not an oversight. The
random v4 job id is itself the capability, because `X-Client-Id` is an anonymous, spoofable
device id and not a security boundary.

### `POST /v1/notes/regenerate`

Regenerates a single note the user rejected in the preview screen. This is a small, cheap,
**synchronous** call — it must return within 10 seconds.

```json
{
  "topic": "Road signs of the Russian traffic code",
  "language": "ru",
  "noteType": "basic",
  "rejectedNote": { "fields": { "front": "…", "back": "…" } },
  "reason": "too_easy"
}
```

`reason` is required and is one of `too_easy`, `too_hard`, `incorrect`, `duplicate`,
`off_topic`, `other`. Collecting it costs the user one tap and is the only feedback signal the
generator gets: it steers what the replacement tests (harder, simpler, a different idea…).

The response is a single `GeneratedNote` of the requested `noteType`, with a new `clientId`. It is
held to the same rules as a note in a job result: its fields are validated for its type, and it
carries at least one source. To have something to cite, the server runs its own small web search
for the topic on every call and the model cites that material by number, exactly as in a
generation job; the regenerated note may therefore cite different pages than the rest of the deck.
A replacement that asks the same thing as the rejected note is discarded. Nothing is cached,
stored or queued: no job is created.

- `noteType: image_occlusion` is rejected with `400 VALIDATION_FAILED`, because the note is built
  on an image the request does not carry.
- `rejectedNote.fields` is treated as untrusted data. It is not validated against `noteType`, and
  only its first 2000 characters are shown to the model.
- `429 RATE_LIMITED` when the client has used up its regeneration budget (see "Quota"), with
  `retryAfterSeconds` set to when the window resets.
- `503 UPSTREAM_UNAVAILABLE` when the search or model provider fails, rejects the request (for
  example a bad API key), answers with malformed data, is too slow for the 10-second budget, or its
  circuit breaker is open; `retryAfterSeconds` is set. A provider problem is never a `500`: that
  status is reserved for a bug in this service.
- `503 NO_VALID_CONTENT` when the search found nothing usable or the model produced no note that
  passes validation. Tapping regenerate again may succeed.

### `GET /v1/health`

Returns `200` with `{ "status": "ok", "version": "…" }`. Used by the client to show an
offline banner before the user spends time filling in the wizard.

`status` is `degraded`, still with `200`, when Postgres or Redis cannot be reached or does not
answer in time. The model, search and image providers are not checked: calling them on every
health request would add their latency and use up their rate limits, and a cheap request that
succeeds says little about whether generation will. A provider outage shows up where it
matters instead: as `PROVIDER_UNAVAILABLE` on the job, or `503 UPSTREAM_UNAVAILABLE` from
`POST /notes/regenerate`. `version` is the server's `DECKLY_VERSION`.

`X-Client-Id` is optional here. When the client sends it, the response also carries that
client's `quota`, so the app can show the remaining budget up front. When it is sent, it is
validated under the same rules as on `POST /generations`, and a malformed value, including an
empty one, is rejected with `400 VALIDATION_FAILED` rather than ignored. The quota is kept in
Redis; when it cannot be read in time, `quota` is left out and `status` is `degraded`, still with
`200`:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "quota": { "limit": 20, "remaining": 17, "resetsAt": "2026-08-15T00:00:00Z" }
}
```

## Quota

A per-client generation budget, counted in **jobs** (not cards) per fixed UTC day and keyed by
`X-Client-Id`.

```json
{ "limit": 20, "remaining": 17, "resetsAt": "2026-08-15T00:00:00Z" }
```

| Field       | Notes                                                                   |
| ----------- | ----------------------------------------------------------------------- |
| `limit`     | Jobs allowed in the current window. Server-side config, not fixed here. |
| `remaining` | Jobs left. `0` means the next `POST /generations` returns `429`.        |
| `resetsAt`  | When the window resets and `remaining` returns to `limit`: next 00:00Z. |

It appears on `GenerationJobCreated` (after each accepted job) and on `Health` (when the request
carried `X-Client-Id`). Exhausting the budget does not change these shapes — the next generation
request is rejected with `429 RATE_LIMITED` and a `retryAfterSeconds` counting down to
`resetsAt`.

The window is a calendar day in UTC, not a rolling 24 hours: every client's budget resets at
00:00Z, and `resetsAt` is the same for every read on the same day, including before the first
job. A client can therefore start up to twice the limit across midnight. Only a new job uses up
the budget. A replay of an earlier request, a request refused with `409` or `429`, and a request
that fails before its job is stored do not.

`X-Client-Id` is chosen by the client, so a client that rotates it gets a fresh budget each time.
As a backstop, jobs are also counted per network address per UTC day, with a coarser limit
(`DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY`, at least the per-client limit). Every IPv6
address in one /64 counts as the same address. Going over it is the same `429 RATE_LIMITED`, even
when `quota.remaining` is above `0`; this budget is not reported in `quota`. When the budget cannot
be checked because Redis does not answer in time, `POST /generations` is refused with
`503 UPSTREAM_UNAVAILABLE` rather than accepted unmetered.

`POST /notes/regenerate` has its own, separate budget and does not use up the job quota. It is
counted per `X-Client-Id` in fixed windows: by default 30 regenerations per hour
(`DECKLY_LIMIT_NOTE_REGENERATIONS_PER_WINDOW`, `DECKLY_LIMIT_NOTE_REGENERATION_WINDOW_SECONDS`).
The window starts on the clock, not at the first call, so a client can make up to twice the limit
across a window boundary. Every call that passes request validation counts, including one that
then fails upstream. Over the budget, the call is rejected with `429 RATE_LIMITED` and
`retryAfterSeconds` until the window resets. This budget is not reported in `quota`.

## Note field shapes

`fields` is an object whose keys depend on `noteType`. The client validates it with a Zod
schema chosen by `noteType`, and rejects a note whose shape does not match rather than
rendering an empty card.

| Note type                 | Fields                                                                                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------- |
| `basic`                   | `front`, `back`                                                                                      |
| `basic_reversed`          | `front`, `back`                                                                                      |
| `basic_optional_reversed` | `front`, `back`, `addReverse` (boolean: `true` also produces the back-to-front card)                 |
| `basic_type_in`           | `front`, `back`                                                                                      |
| `cloze`                   | `text` containing `{{c1::…}}` markers, optional `extra`                                              |
| `multiple_choice`         | `question`, `answer`, `distractors` (2–4 strings)                                                    |
| `image_occlusion`         | `imageId`, `regions` (array of `{ ordinal, x, y, width, height }`, normalised 0–1), optional `extra` |

Rules the backend must uphold:

- `basic_optional_reversed` always carries `addReverse` as a JSON boolean, never as a string
  or a number.
- Cloze text contains at least one `{{cN::…}}` marker, numbered from 1 with no gaps.
- Multiple-choice `distractors` are plausible, mutually exclusive, and never contain the
  correct answer.
- Image-occlusion `imageId` is the `mediaId` of one of the note's **own** `media` entries of
  kind `image`, spelled exactly as that `mediaId` is (canonical lowercase UUID). A note whose
  `imageId` matches none of them is rejected.
- Image-occlusion regions have at least one entry, and their `ordinal` values are unique and
  at least 1. They need not be consecutive: `1, 3, 7` is valid, `1, 1` is not.
- Every image-occlusion region lies entirely inside the image: `x`, `y`, `width` and `height`
  are each within 0–1, `x + width ≤ 1`, `y + height ≤ 1`, and neither `width` nor `height` is
  zero.
- `clientId` is unique within a result and is what the client uses to track edits and
  regenerations in the preview screen.

## Sources

Every note in a result carries at least one source in `sources`. They are the
anti-hallucination guard and a product feature: the preview screen shows them so the user can
verify a card before saving it.

- `sources` is never empty. A note the backend cannot source is **dropped**, the same
  repair-or-drop posture as any other invalid note: only that note is removed, and the job
  still succeeds with the notes that remain.
- Each source has a non-blank `title` and an `http(s)` `url`. `retrievedAt`, when present,
  carries an explicit offset.

## Media

```json
{
  "mediaId": "…",
  "kind": "image",
  "url": "https://…",
  "alt": "Warning sign",
  "width": 640,
  "height": 640,
  "license": "CC-BY-4.0",
  "attribution": {
    "author": "Jane Doe",
    "title": "Warning sign",
    "sourceUrl": "https://commons.wikimedia.org/wiki/File:Warning_sign.jpg",
    "licenseUrl": "https://creativecommons.org/licenses/by/4.0/"
  }
}
```

- URLs must be directly downloadable without authentication and stay valid for at least 24
  hours, since the client caches media locally for offline review.
- `alt` is mandatory on images. It is the accessibility text and it is also what the client
  falls back to when a download fails.
- `license` is required. The user is shown it, and content with no known licence must not be
  returned at all. It is an SPDX identifier: `CC0-1.0`, `Public-Domain`, or a generic (unported)
  `CC-BY-<version>` or `CC-BY-SA-<version>`. Ported variants such as CC BY-SA 3.0 DE, and any
  NonCommercial or NoDerivatives licence, are not returned.
- `attribution` is present exactly when the licence requires it (CC BY and CC BY-SA) and absent
  otherwise. It carries `author` (the credit line as the author asks for it), `title`,
  `sourceUrl` (the page the work comes from) and `licenseUrl` (the licence deed), all required.
  The client must show the attribution with the image. An image whose attribution cannot be
  derived from the provider's metadata is not returned; the backend never guesses an author.
- Every image is screened before it is returned. A classifier judges each candidate's title,
  description and categories against the note it would illustrate, for both the content policy
  and relevance: a picture that only shows the subject in passing, or shows something else, is
  not attached. The image policy adds to the content policy: photographs showing nudity, sexual
  activity, graphic injury, gore or a dead body are always blocked; anatomical diagrams,
  clinical illustrations and artworks are allowed only when the note's own subject calls for
  them. A note whose candidates are all rejected gets no image.
- Images are best effort. If no image with a known licence is found for a note, or the image
  provider is unavailable, the note is returned without media and the job still succeeds; an
  image outage never fails a job with `PROVIDER_UNAVAILABLE`. The same holds when the image
  classifier is unavailable: no image ships unscreened, so the job succeeds without images.

## Errors

RFC 9457 problem details, with a stable machine-readable `code` the client maps to a
translated message. The client never displays `detail` directly — that string is not
localised and is for logs.

```json
{
  "type": "https://api.example.com/problems/rate-limited",
  "title": "Rate limited",
  "status": 429,
  "detail": "Too many generation jobs for this client",
  "code": "RATE_LIMITED",
  "retryAfterSeconds": 60
}
```

| Code                       | Status              | Meaning                                           |
| -------------------------- | ------------------- | ------------------------------------------------- |
| `VALIDATION_FAILED`        | 400                 | Request body failed validation                    |
| `TOPIC_REJECTED`           | 422                 | Topic violates the content policy                 |
| `RATE_LIMITED`             | 429                 | A job or regeneration budget is used up; "Quota"  |
| `JOB_NOT_FOUND`            | 404                 | Unknown job id                                    |
| `JOB_ALREADY_TERMINAL`     | 409                 | Cancel on a finished job                          |
| `IDEMPOTENCY_KEY_CONFLICT` | 409                 | `Idempotency-Key` reused with a different request |
| `PROVIDER_UNAVAILABLE`     | 200 in the job body | The model or search provider failed the job       |
| `NO_VALID_CONTENT`         | 200 in the job body | Every note was dropped; nothing safe to return    |
| `NO_VALID_CONTENT`         | 503 on regenerate   | No valid replacement note could be produced       |
| `GENERATION_FAILED`        | 200 in the job body | The job failed for any other reason               |
| `UPSTREAM_UNAVAILABLE`     | 503                 | Model, search provider or quota store is down     |
| `ROUTE_NOT_FOUND`          | 404                 | No endpoint exists at this path                   |
| `METHOD_NOT_ALLOWED`       | 405                 | Endpoint exists but not for this HTTP method      |
| `INTERNAL_ERROR`           | 500                 | Anything else                                     |

Note the three "200 in the job body" rows: a **failed job is not an HTTP error**. `GET
/generations/{jobId}` returns `200` with `status: "failed"` and a populated `error` object.
Returning a 5xx for a failed job would make the client's polling logic conflate a transport
problem with a generation problem.

A failed job's `error.code` is always exactly one of these three, never any other code:

- `PROVIDER_UNAVAILABLE` — an upstream model or search provider failed or timed out, and
  retrying did not help. The same request may succeed later.
- `NO_VALID_CONTENT` — generation ran, but every note was dropped during validation (see
  "Sources" and "Note field shapes"), so there is nothing safe to return. A result is never
  returned with zero notes.
- `GENERATION_FAILED` — the catch-all for any other failure.

`PROVIDER_UNAVAILABLE` and `UPSTREAM_UNAVAILABLE` describe the same kind of outage at different
points. `UPSTREAM_UNAVAILABLE` is an HTTP `503` on a request the server could not serve at all.
`PROVIDER_UNAVAILABLE` is inside the body of a job that was already accepted and later failed.

`ROUTE_NOT_FOUND` and `JOB_NOT_FOUND` share the 404 status, so the client must branch on
`code`, not on status. `ROUTE_NOT_FOUND` means the client called a path the server does not
serve, which is a client or deployment bug, not a missing job. `METHOD_NOT_ALLOWED` responses
carry an `Allow` header listing the methods the path does accept.

## Non-functional requirements

- `POST /generations` responds in under 500 ms plus the topic check, which is bounded by
  `DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS` (default 2 s). Apart from that check it only
  enqueues. A check that does not finish in time is `503 UPSTREAM_UNAVAILABLE`, never a silent
  pass.
- `GET /generations/{jobId}` responds in under 200 ms and is cheap enough to poll every 2
  seconds per client.
- Jobs are retained for at least 24 hours after completion so a user who backgrounded the
  app can come back to a finished result. After that they are deleted, and polling one returns
  `404 JOB_NOT_FOUND`.
- Identical `(topic, language, cardCount, difficulty, noteTypes)` tuples should hit a server
  side cache. The same "table of irregular verbs" is requested by many users and should be
  generated once. `includeImages` and `instructions` shape the output too, so they are part of
  the cache key. A cache hit is still a job: it goes `queued → running → succeeded` and is polled
  like any other, and it still counts against the client's daily quota.
- Generated content must be filtered for policy violations before it reaches the client.

## Resolved decisions

1. **Topic and retrieval — open-domain.** Any topic the user types is accepted; the model
   writes the questions and cards itself rather than copying them from a curated per-subject
   provider set. That does not make sources optional: every returned note still carries at
   least one source the user can check its claims against, and a note the backend cannot
   source is dropped (see "Sources" below). The content policy still applies: a topic that
   violates it is rejected with `TOPIC_REJECTED`, and generated content is filtered before it
   reaches the client.
2. **Images — found by the model / image search.** `includeImages` triggers the model to source
   images. The contract's media rules are the hard constraint on this: every returned image
   carries a real `license` and `alt`, and an image whose licence cannot be established is not
   returned at all — the note degrades to no image rather than shipping unlicensed media. How the
   licence is derived is the backend's choice, but it must be derivable, or the image is dropped.
3. **Per-client quota — yes, N generations per day per client (specced).** The quota counts
   generation **jobs** (not cards) per fixed UTC day, keyed by `X-Client-Id`. It is now in
   `openapi.yaml` as a `Quota` object `{ limit, remaining, resetsAt }`:
   - returned on `GenerationJobCreated`, so the client learns the remaining budget after each
     accepted job;
   - returned on `Health` **when the request carries `X-Client-Id`** (optional there), so the
     client can show the budget before the wizard and disable the button at zero;
   - once the budget is exhausted, the next `POST /generations` gets `429 RATE_LIMITED` with
     `retryAfterSeconds`, as before.

   The concrete `limit` value is server-side config, not part of the schema. The mobile client
   already accepts these fields; surfacing them in the UI is a later frontend task.

4. **No streaming — batch result.** The job returns its full result at once; the preview screen
   shows all cards together, and every card is editable before the deck is saved. The current
   contract (`GenerationJob.result` populated on `succeeded`) already covers this; no change
   needed.
5. **No streaming — batch result.** The job returns its full result at once; the preview screen
   shows all cards together, and every card is editable before the deck is saved. The current
   contract (`GenerationJob.result` populated on `succeeded`) already covers this; no change
   needed.
