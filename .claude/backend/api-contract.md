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
accepted. See "Quota" below.

### `GET /v1/generations/{jobId}`

Polled by the client. Returns `200` in every non-terminal and terminal state; a job that
does not exist returns `404`.

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

`reason` is one of `too_easy`, `too_hard`, `incorrect`, `duplicate`, `off_topic`, `other`.
Collecting it costs the user one tap and is the only feedback signal the generator gets.

### `GET /v1/health`

Returns `200` with `{ "status": "ok", "version": "…" }`. Used by the client to show an
offline banner before the user spends time filling in the wizard.

`X-Client-Id` is optional here. When the client sends it, the response also carries that
client's `quota`, so the app can show the remaining budget up front:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "quota": { "limit": 20, "remaining": 17, "resetsAt": "2026-08-15T00:00:00Z" }
}
```

## Quota

A per-client generation budget, counted in **jobs** (not cards) over a rolling day and keyed by
`X-Client-Id`.

```json
{ "limit": 20, "remaining": 17, "resetsAt": "2026-08-15T00:00:00Z" }
```

| Field       | Notes                                                                   |
| ----------- | ----------------------------------------------------------------------- |
| `limit`     | Jobs allowed in the current window. Server-side config, not fixed here. |
| `remaining` | Jobs left. `0` means the next `POST /generations` returns `429`.        |
| `resetsAt`  | When the window resets and `remaining` returns to `limit`.              |

It appears on `GenerationJobCreated` (after each accepted job) and on `Health` (when the request
carried `X-Client-Id`). Exhausting the budget does not change these shapes — the next generation
request is rejected with `429 RATE_LIMITED` and a `retryAfterSeconds`.

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
  "license": "CC-BY-4.0"
}
```

- URLs must be directly downloadable without authentication and stay valid for at least 24
  hours, since the client caches media locally for offline review.
- `alt` is mandatory on images. It is the accessibility text and it is also what the client
  falls back to when a download fails.
- `license` is required. The user is shown it, and content with no known licence must not be
  returned at all.

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
| `RATE_LIMITED`             | 429                 | Too many jobs for this client                     |
| `JOB_NOT_FOUND`            | 404                 | Unknown job id                                    |
| `JOB_ALREADY_TERMINAL`     | 409                 | Cancel on a finished job                          |
| `IDEMPOTENCY_KEY_CONFLICT` | 409                 | `Idempotency-Key` reused with a different request |
| `PROVIDER_UNAVAILABLE`     | 200 in the job body | The model or search provider failed the job       |
| `NO_VALID_CONTENT`         | 200 in the job body | Every note was dropped; nothing safe to return    |
| `GENERATION_FAILED`        | 200 in the job body | The job failed for any other reason               |
| `UPSTREAM_UNAVAILABLE`     | 503                 | Model or search provider is down                  |
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

- `POST /generations` responds in under 500 ms; it only enqueues.
- `GET /generations/{jobId}` responds in under 200 ms and is cheap enough to poll every 2
  seconds per client.
- Jobs are retained for at least 24 hours after completion so a user who backgrounded the
  app can come back to a finished result.
- Identical `(topic, language, cardCount, difficulty, noteTypes)` tuples should hit a server
  side cache. The same "table of irregular verbs" is requested by many users and should be
  generated once.
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
   generation **jobs** (not cards) over a rolling day, keyed by `X-Client-Id`. It is now in
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
