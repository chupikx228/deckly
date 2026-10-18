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

Uploaded documents (`POST /documents`) and recordings (`POST /recordings`, or fetched with
`POST /recordings/from-url`) are not user data the backend owns. They are short-lived
inputs to generation, readable only by the client that uploaded them, and deleted 24 hours after
upload unless a generation uses them; a document a generation used is deleted once the last job
that used it is.

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
| `topic`         | one of   | 3–200 characters after trimming, at least 3 of them visible (see below) |
| `documentId`    | one of   | A `documentId` from `POST /documents` by the same client (see below)    |
| `language`      | yes      | BCP 47 tag. The language of the **cards**, not the interface.           |
| `cardCount`     | yes      | 5–200. A target, not a guarantee; the response may return fewer.        |
| `difficulty`    | no       | `beginner` \| `intermediate` \| `advanced`. Defaults to `intermediate`. |
| `noteTypes`     | no       | Which types the generator may produce. Defaults to `["basic"]`.         |
| `includeImages` | no       | Defaults to `false`. Images add significant latency and cost.           |
| `instructions`  | no       | Free-form user steering, up to 500 characters.                          |

**Exactly one of `topic` and `documentId`.** A request names what to generate from: a `topic`,
researched on the web, or a `documentId`, a document the same `X-Client-Id` uploaded with
`POST /documents`, whose text the cards are written from and whose pages or slides they cite (see
"Sources"). A request with both, with neither, or with either one sent as `null` is
`400 VALIDATION_FAILED`. `documentId` must be a lowercase, hyphenated version 4 UUID, like the
headers. Everything else in the request means the same for both.

A `documentId` the client cannot use is `404 DOCUMENT_NOT_FOUND`: unknown, uploaded by another
client, still being uploaded, or expired (see `GET /v1/documents/{documentId}`). It is checked
before the quota is touched, so the refusal costs nothing. A document that a job has used stays
usable for as long as that job can be polled, so a client can generate from it again with other
settings. Generating from a document spends one unit of the generation quota like any other job;
the upload spent its own budget once and is not charged again. The content check judges the
document's file name together with `instructions` (a block is `422 TOPIC_REJECTED`), and the
worker checks the document's text before any card is written from it; a document that violates
the policy fails the job with `DOCUMENT_REJECTED`. A `documentId` that names a recording
(`POST /recordings` or `POST /recordings/from-url`) is transcribed by the worker and generated from
like a document, and its notes cite `transcript` sources (below). A deployment that runs without
transcription (`DECKLY_TRANSCRIPTION_ENABLED=false`) answers it `422
DOCUMENT_NOT_SUPPORTED_FOR_GENERATION`, checked with the document lookup before the quota is
touched. A job from a recording takes minutes rather than seconds: the worker transcribes at
roughly a fifth of real time, so a 15-minute recording spends a few minutes in
`parsing_sources`. The job fails `DOCUMENT_UNREADABLE` when its audio cannot be decoded,
`DOCUMENT_HAS_NO_TEXT` when it contains no speech, and `PROVIDER_UNAVAILABLE` when transcription
fails or runs out of time. A result for the same `documentId` and the same settings may
be served from the cache, like a topic.

The mobile app does not send `documentId` yet; DEC-77 adds it and regenerates
`@deckly/api-contract` from `openapi.yaml`.

**`image_occlusion` is not generated yet.** A request whose `noteTypes` contains
`image_occlusion` is rejected up front with `400 VALIDATION_FAILED`, whatever `includeImages`
says, and no job is created. The design is decided and the implementation deferred; see
"Resolved decisions", item 5. Once it is generated, it will also need `includeImages: true`,
because the note is built on an image the server fetches. The schema already expresses that
part with `if`/`then` on `GenerationRequest`, so a request without images is rejected by the
schema and a request with images by the server.

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
  though it is in the enum. Every type in the enum is currently supported except
  `image_occlusion` (see above).

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
- A request the server no longer accepts cannot be replayed. Sending it again fails validation
  (`400 VALIDATION_FAILED`) before the key is looked up. Reusing its key for a valid request is
  `409 IDEMPOTENCY_KEY_CONFLICT`, because the two requests cannot be the same. A request that
  named `image_occlusion` before it was withdrawn is the case this covers.
- A key is honoured for 24 hours after the original request
  (`DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS`). After that the same key starts a new job, as if
  it had never been used. The original job is not affected and can still be polled until it is
  deleted (see "Non-functional requirements"). Expired keys are released by a scheduled task,
  so a key can keep replaying for up to one sweep interval past the 24 hours.

Requests are compared after defaults are applied and `topic` is trimmed. Key order, defaults
sent explicitly, and whitespace around the topic therefore do not count as differences. A
`documentId` request and a `topic` request are never the same request.
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
          "kind": "web",
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

The content policy applies here as it does to a job, at both ends. The topic and the rejected
note's fields (its first 2000 characters, exactly what the model is shown) are screened together,
because the rejected note steers the model the way `instructions` do in a job. The replacement
note is screened again before it is returned. Both checks fail closed: a classifier refusal
counts as a block.

- `noteType: image_occlusion` is rejected with `400 VALIDATION_FAILED`, because the note is built
  on an image the request does not carry.
- **A deck generated from a document cannot be regenerated in v1.** The request names exactly one
  of `topic` and `documentId`, like `POST /v1/generations`: a request with both, with neither, or
  with either one sent as `null` is `400 VALIDATION_FAILED`. A client sends `documentId`, the one its
  generation job was created with, instead of a `topic` for a note of a deck generated from an
  uploaded document, and gets `422 DOCUMENT_REGENERATION_UNSUPPORTED`. The refusal is decided from
  the request alone, before the regeneration budget is touched and before any search, document read
  or content check, so it costs nothing and does not say whether the document still exists. The
  client should not offer regenerate on such a deck; it can still edit or delete the note. See
  "Resolved decisions", item 6.
- `rejectedNote.fields` is treated as untrusted data. It is not validated against `noteType`, and
  only its first 2000 characters are shown to the model.
- `422 TOPIC_REJECTED` when the topic or the rejected note violates the content policy. The call
  still counts against the regeneration budget.
- `429 RATE_LIMITED` when the client has used up its regeneration budget (see "Quota"), with
  `retryAfterSeconds` set to when the window resets.
- `503 UPSTREAM_UNAVAILABLE` when the search, model or content-check provider fails, rejects the
  request (for example a bad API key), answers with malformed data, is too slow for the 10-second
  budget, or its circuit breaker is open; `retryAfterSeconds` is set. A provider problem is never a
  `500`: that status is reserved for a bug in this service.
- `503 NO_VALID_CONTENT` when the search found nothing usable, the model produced no note that
  passes validation, or the content check did not allow the note it produced. The note is never
  returned. Tapping regenerate again may succeed.

### `POST /v1/documents`

Uploads one PDF or PowerPoint (`.pptx`) file to generate from and returns `201` with its
metadata. The file goes in a `multipart/form-data` body as the only part, named `file`.
`X-Client-Id` is required, under the same rules as on `POST /generations`.

```json
{
  "documentId": "…",
  "filename": "lecture-3.pdf",
  "sizeBytes": 482113,
  "contentType": "application/pdf",
  "uploadedAt": "2026-08-14T10:30:00Z"
}
```

- **Type is decided by content.** The server reads the file itself and never trusts its name or
  the declared `Content-Type`. A PDF must start with a `%PDF-1.x` or `%PDF-2.x` header within its
  first 1024 bytes and end with `%%EOF` within its last 1024. A presentation must be an Office
  Open XML package whose main part is `ppt/presentation.xml` with the presentation content type.
  Everything else is `415 UNSUPPORTED_DOCUMENT_TYPE`: other files renamed to `.pdf`, Word and
  Excel files, legacy binary `.ppt`, macro-enabled `.pptm`, slide shows (`.ppsx`), templates,
  password-protected presentations, damaged or truncated files and empty files. `contentType`
  in the response is the detected type.
- **Size.** At most 25 MiB (`DECKLY_LIMIT_DOCUMENT_MAX_BYTES`). A larger file is
  `413 DOCUMENT_TOO_LARGE`, refused from the `Content-Length` header when it is sent and
  otherwise as soon as the body passes the limit. Nothing is stored either way.
- **Filename.** Kept for display only: cut to its last path component and 255 characters, with
  control and invisible characters removed (including bidi overrides). A name with nothing left
  is `400 VALIDATION_FAILED`. It is never used to name the stored object.
- **Malformed requests** are `400 VALIDATION_FAILED`: a body that is not `multipart/form-data`,
  no part named `file`, more than one file, or any other form field. So is a body that is not
  fully received within `DECKLY_LIMIT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS` (120 by default),
  counted from the moment the upload is admitted to the last byte, however steadily the client is
  sending: the server stops reading, frees the upload slot and answers `400`. The upload was
  counted against the client's budget.
- `429 RATE_LIMITED` when the client's upload budget is used up (see "Quota").
- `503 UPSTREAM_UNAVAILABLE` when the object store or the rate-limit store fails or is too slow,
  or when the server is already handling its maximum number of uploads at once
  (`DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY`, 4 by default, per API process). The latter is
  answered immediately, never queued, with `Retry-After: 5` and `retryAfterSeconds`, before the
  body is read and before the upload is counted against the client's budget, so retrying costs
  nothing. A failed upload leaves nothing behind that a client can read.
- **No idempotency key.** Retrying an upload stores a second copy with a new `documentId`; the
  copy nothing uses expires like any other.

### `POST /v1/recordings`

Uploads one audio or video recording to generate from later and returns `201` with the same metadata
body as `POST /documents`, plus `durationSeconds`. Same request shape (`multipart/form-data`, one
part named `file`, `X-Client-Id` required), same filename rules, same malformed-request `400`s and
same `GET /documents/{documentId}` to read it back. The reply's `documentId` is what a later request
refers to.

```json
{
  "documentId": "…",
  "filename": "lecture-3.m4a",
  "sizeBytes": 7308,
  "contentType": "audio/mp4",
  "uploadedAt": "2026-08-14T10:30:00Z",
  "durationSeconds": 2.064
}
```

- **Type is decided by content.** MP3 (MPEG-1/2/2.5 layer III, optionally ID3-tagged), M4A
  (`audio/mp4`), WAV (uncompressed PCM or floating point), MP4 (`video/mp4`) and MOV
  (`video/quicktime`). Everything else is `415 UNSUPPORTED_DOCUMENT_TYPE`: PDFs and other files
  renamed to `.mp3`, Ogg/Opus, WebM, FLAC, AMR, 3GP, raw AAC, DRM-protected audio,
  HEIC and AVIF images (they share the MP4 container), a video with no audio track, a recording
  whose metadata is internally inconsistent, and damaged or truncated files. `contentType` is the
  detected type. A recording sent to `POST /documents` is `415` there too.
- **Duration.** `durationSeconds` is read from the container's metadata, never by decoding the
  audio, and is never shorter than what a decoder would play. A recording longer than
  `DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS` (15 minutes by default) is
  `422 RECORDING_TOO_LONG`. The cap is expected to rise once transcription is not bound to
  self-hosted CPU. Transcription stops at the same cap measured in decoded audio, so a file whose
  metadata understates its length is transcribed only up to the cap.
- **Size.** At most 48 MiB (`DECKLY_LIMIT_RECORDING_MAX_BYTES`), `413 DOCUMENT_TOO_LARGE` otherwise,
  enforced exactly as for documents.
- **Budget and capacity.** Recording uploads count against the same upload budget as documents
  (`429 RATE_LIMITED`). They have their own concurrency limit
  (`DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY`, 2 by default, per API process): a full one is
  `503 UPSTREAM_UNAVAILABLE` with `Retry-After: 5`, refused before the body is read or the budget
  is spent, and does not affect document uploads. The body must arrive within
  `DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS` (240 by default), else `400`.
- **Retention** is the same as for documents.
- **A generation source.** `POST /generations` with a recording's `documentId` transcribes it and
  generates from the transcript; see `POST /v1/generations`.

### `POST /v1/recordings/from-url`

Fetches the audio of one YouTube video and stores it as a recording. Returns `201` with the same
metadata body as `POST /recordings`; the reply's `documentId` is used exactly like an uploaded
recording's, and nothing downstream can tell the two apart. `X-Client-Id` is required.

```json
{ "url": "https://youtu.be/jNQXAC9IVRw" }
```

```json
{
  "documentId": "…",
  "filename": "Me at the zoo.m4a",
  "sizeBytes": 309288,
  "contentType": "audio/mp4",
  "uploadedAt": "2026-10-17T10:30:00Z",
  "durationSeconds": 19.064
}
```

- **Which links.** A link to a single video on `youtube.com`, `www.youtube.com`, `m.youtube.com`,
  `music.youtube.com` (`/watch?v=…`, `/shorts/…`, `/live/…`, `/embed/…`) or `youtu.be/…`, `http` or
  `https`, with no credentials or port. Extra query parameters (`t`, `list`, `si`) are ignored.
  Anything else — other sites, playlists and channels, IP addresses, look-alike hosts — is
  `422 UNSUPPORTED_RECORDING_URL`, decided before anything is fetched, counted or queued. Only the
  11-character video id is taken from the link; the server builds the YouTube URL itself.
- **Body.** JSON with exactly one field, `url` (1–2048 characters). Anything else is
  `400 VALIDATION_FAILED`.
- **Duration.** Checked twice: from YouTube's metadata before any audio is downloaded, and from the
  stored file with the same probe as uploads. Over the cap either way is `422 RECORDING_TOO_LONG`.
  `durationSeconds` comes from the file, not from YouTube.
- **Unavailable videos.** A private, removed, age-restricted, region-blocked or members-only video,
  a live stream, an upcoming premiere, and a video without an audio-only M4A stream are
  `422 RECORDING_UNAVAILABLE`.
- **Size.** The audio counts against the recording size limit (48 MiB by default):
  `413 DOCUMENT_TOO_LARGE`, refused from the metadata when YouTube states a size, and otherwise as
  soon as the download passes the limit.
- **Budget and capacity.** URL fetches have their **own** budget, separate from uploads: 5 per hour
  per `X-Client-Id`, 20 per network address and 200 per hour for the whole server
  (`429 RATE_LIMITED`, see "Quota"). Their own concurrency limit
  (`DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY`, 1 by default, per API process) is
  `503 UPSTREAM_UNAVAILABLE` with `Retry-After: 5` when full, before the budget is spent.
- **Slow by nature.** The response is sent once the audio is stored, typically a few seconds and at
  most `DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS` (120 by default). A fetch that does not finish in
  time, that YouTube refuses (YouTube blocks many datacenter addresses), or that fails for any other
  upstream reason is `503 UPSTREAM_UNAVAILABLE` with `Retry-After: 60`; the attempt was counted.
- **Retention** is the same as for documents. **No idempotency key**: retrying stores a second copy.
- **Not yet a generation source**, like any recording (DEC-86).

### `GET /v1/documents/{documentId}`

Returns `200` with the same metadata body as the upload. It never returns the file itself.
`durationSeconds` is present only for a recording.

**Access.** Unlike a job, a document is scoped to the client that uploaded it: the request must
carry that client's `X-Client-Id`. Any other client, an unknown or malformed id, a document still
being uploaded, and one that has expired all get the same `404 DOCUMENT_NOT_FOUND`, so the
response does not reveal whether a document exists. This differs deliberately from
`GET /generations/{jobId}`: a document carries the user's own content and file name.

**Retention.** A document that nothing uses expires 24 hours after it was uploaded
(`DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS`), and reading it or generating from it afterwards is
`404`, even before the scheduled task has deleted it. A document a generation job used stays
readable, and usable, for as long as any job that used it can be polled; it is deleted once job
retention has deleted the last of them, within one sweep interval.

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
address in one /56 counts as the same address. Going over it is the same `429 RATE_LIMITED`, even
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

`POST /documents` and `POST /recordings` share a third budget, counted in fixed windows like
regeneration: by default 10 uploads per hour per `X-Client-Id`
(`DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_WINDOW`, `DECKLY_LIMIT_DOCUMENT_UPLOAD_WINDOW_SECONDS`) and, as
a backstop against rotated client ids, 50 per hour per network address
(`DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_ADDRESS_PER_WINDOW`, IPv6 grouped by /56). Every upload with
valid headers counts, including one later refused for its size or type. The budget is checked before
the body is read, and an upload whose `Content-Length` is already over the size limit is refused
before it is counted. When Redis cannot be reached in time the upload is `503 UPSTREAM_UNAVAILABLE`,
never accepted unmetered. This budget is not reported in `quota`.

`POST /recordings/from-url` has a fourth budget of its own, because every fetch makes the server
download from YouTube on the client's behalf: by default 5 per hour per `X-Client-Id`
(`DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_WINDOW`), 20 per network address
(`DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW`, IPv6 grouped by /56) and 200 for the
whole server (`DECKLY_LIMIT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW`), in fixed one-hour windows
(`DECKLY_LIMIT_RECORDING_URL_WINDOW_SECONDS`). Every request with a supported link counts, including
one later refused for its duration, size or availability; an unsupported link and a request refused
because every fetch slot is busy do not. When Redis cannot be reached in time the fetch is
`503 UPSTREAM_UNAVAILABLE`. This budget is not reported in `quota`.

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
- No result carries an `image_occlusion` note while its generation is deferred (see "Resolved
  decisions", item 5). The rules above stay fixed for when it ships, which also adds an optional
  `label` to each region.
- `clientId` is unique within a result and is what the client uses to track edits and
  regenerations in the preview screen.

## Sources

Every note in a result carries at least one source in `sources`. They are the
anti-hallucination guard and a product feature: the preview screen shows them so the user can
verify a card before saving it.

- `sources` is never empty. A note the backend cannot source is **dropped**, the same
  repair-or-drop posture as any other invalid note: only that note is removed, and the job
  still succeeds with the notes that remain.
- Every source has a `kind` and a non-blank `title`. The client branches on `kind` and ignores a
  source whose `kind` it does not know, so new kinds can be added without breaking old builds.
- `kind: "web"` is a web page: an `http(s)` `url`, and `retrievedAt`, when present, carries an
  explicit offset.
- `kind: "document"` is a page or slide of a document the client uploaded: `documentId` (as
  returned by `POST /documents`), `title` (the file name), `unit` (`page` for a PDF, `slide` for a
  presentation) and `number`, 1-based as the document's own viewer numbers it. A note may cite
  several pages, or a page and a web source.
- `kind: "transcript"` is a passage of a recording the client uploaded or fetched: `documentId`,
  `title` (the file name), and `startMs` and `endMs`, the passage's time range in milliseconds
  from the start of the recording (`endMs` is always after `startMs`). A passage is about a minute
  of speech, cut where one transcribed segment ends and the next begins, so the range points the
  user at the moment to play back.

```json
{ "kind": "document", "title": "lecture-3.pdf", "documentId": "…", "unit": "page", "number": 12 }
{ "kind": "transcript", "title": "lecture-3.m4a", "documentId": "…", "startMs": 62480, "endMs": 121900 }
```

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

| Code                                    | Status              | Meaning                                                 |
| --------------------------------------- | ------------------- | ------------------------------------------------------- |
| `VALIDATION_FAILED`                     | 400                 | Request body failed validation                          |
| `TOPIC_REJECTED`                        | 422                 | Topic, file name or rejected note violates policy       |
| `RATE_LIMITED`                          | 429                 | A job, regeneration or upload budget; "Quota"           |
| `JOB_NOT_FOUND`                         | 404                 | Unknown job id                                          |
| `JOB_ALREADY_TERMINAL`                  | 409                 | Cancel on a finished job                                |
| `IDEMPOTENCY_KEY_CONFLICT`              | 409                 | `Idempotency-Key` reused with a different request       |
| `PROVIDER_UNAVAILABLE`                  | 200 in the job body | The model or search provider failed the job             |
| `NO_VALID_CONTENT`                      | 200 in the job body | Every note was dropped; nothing safe to return          |
| `NO_VALID_CONTENT`                      | 503 on regenerate   | No valid replacement note could be produced             |
| `DOCUMENT_UNREADABLE`                   | 200 in the job body | Uploaded document is damaged or over a limit            |
| `DOCUMENT_ENCRYPTED`                    | 200 in the job body | Uploaded PDF needs a password to open                   |
| `DOCUMENT_HAS_NO_TEXT`                  | 200 in the job body | Uploaded document has no extractable text               |
| `DOCUMENT_NOT_FOUND`                    | 200 in the job body | Uploaded document was gone when the job read it         |
| `DOCUMENT_REJECTED`                     | 200 in the job body | Uploaded document's text violates content policy        |
| `DOCUMENT_REGENERATION_UNSUPPORTED`     | 422 on regenerate   | A note of a document-sourced deck cannot be regenerated |
| `DOCUMENT_NOT_SUPPORTED_FOR_GENERATION` | 422                 | A recording, on a deployment without transcription      |
| `GENERATION_FAILED`                     | 200 in the job body | The job failed for any other reason                     |
| `UPSTREAM_UNAVAILABLE`                  | 503                 | A provider, quota store or object store is down         |
| `DOCUMENT_NOT_FOUND`                    | 404                 | Unknown, foreign or expired document id                 |
| `DOCUMENT_TOO_LARGE`                    | 413                 | Uploaded document is over the size limit                |
| `UNSUPPORTED_DOCUMENT_TYPE`             | 415                 | Uploaded file is not a type the endpoint accepts        |
| `RECORDING_TOO_LONG`                    | 422                 | Uploaded or fetched recording is over the duration cap  |
| `UNSUPPORTED_RECORDING_URL`             | 422                 | The link is not to a single YouTube video               |
| `RECORDING_UNAVAILABLE`                 | 422                 | The video is private, removed, restricted or live       |
| `ROUTE_NOT_FOUND`                       | 404                 | No endpoint exists at this path                         |
| `METHOD_NOT_ALLOWED`                    | 405                 | Endpoint exists but not for this HTTP method            |
| `INTERNAL_ERROR`                        | 500                 | Anything else                                           |

Note the "200 in the job body" rows: a **failed job is not an HTTP error**. `GET
/generations/{jobId}` returns `200` with `status: "failed"` and a populated `error` object.
Returning a 5xx for a failed job would make the client's polling logic conflate a transport
problem with a generation problem.

A failed job's `error.code` is always exactly one of these, never any other code:

- `PROVIDER_UNAVAILABLE` — an upstream model or search provider failed or timed out, and
  retrying did not help. The same request may succeed later.
- `NO_VALID_CONTENT` — generation ran, but every note was dropped during validation (see
  "Sources" and "Note field shapes"), so there is nothing safe to return. A result is never
  returned with zero notes.
- `DOCUMENT_UNREADABLE` — the uploaded document the job reads cannot be parsed: it is damaged,
  or it trips a safety limit (an archive that inflates far past its size, too many pages or
  parts, embedded DTDs, extraction that runs too long). Re-exporting the file usually fixes it.
- `DOCUMENT_ENCRYPTED` — the uploaded PDF needs a password to open. A PDF that only restricts
  editing or printing opens without one and is read normally, except one encrypted with AES,
  which this service does not decrypt yet and also reports here. The user should remove the
  protection and upload again.
- `DOCUMENT_HAS_NO_TEXT` — the uploaded document has fewer than 50 visible characters of
  extractable text: a scan or photos without a text layer, slides that are only pictures, or an
  empty file. There is no OCR. Pages and slides without text are skipped silently as long as the
  document as a whole has text.
- `DOCUMENT_NOT_FOUND` — the uploaded document was no longer in storage when the worker read it.
  A document cannot expire while a job uses it, so this means it was removed outside the normal
  lifecycle; generating from the same `documentId` again will fail the same way, and the user
  should upload the file again.
- `DOCUMENT_REJECTED` — the text of the uploaded document violates the content policy, so no card
  was written from it. Like `TOPIC_REJECTED`, it spends the quota unit.
- `GENERATION_FAILED` — the catch-all for any other failure.

The five `DOCUMENT_*` codes appear only on jobs that generate from an uploaded document; a client
must still handle any of them on any failed job. `DOCUMENT_NOT_FOUND` is also the `404` of
`POST /generations` and the document endpoints; in a job body it has status `200`, like every
failure code.

`PROVIDER_UNAVAILABLE` and `UPSTREAM_UNAVAILABLE` describe the same kind of outage at different
points. `UPSTREAM_UNAVAILABLE` is an HTTP `503` on a request the server could not serve at all.
`PROVIDER_UNAVAILABLE` is inside the body of a job that was already accepted and later failed.

`ROUTE_NOT_FOUND`, `JOB_NOT_FOUND` and `DOCUMENT_NOT_FOUND` share the 404 status, so the client must branch on
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
- An uploaded document nothing uses is deleted 24 hours after upload, and one a job used once
  job retention has deleted the last job that used it. The object store is never exposed to the
  client; documents are read only through the API.
- Identical `(documentId, language, cardCount, difficulty, noteTypes, includeImages,
instructions)` requests may share a cached result like topic requests. Only the client that
  uploaded a document can name it, so a document's cached results are never served to anyone
  else.

## Resolved decisions

1. **Topic and retrieval — open-domain.** Any topic the user types is accepted; the model
   writes the questions and cards itself rather than copying them from a curated per-subject
   provider set. That does not make sources optional: every returned note still carries at
   least one source the user can check its claims against, and a note the backend cannot
   source is dropped (see "Sources" below). The content policy still applies: a topic that
   violates it is rejected with `TOPIC_REJECTED`, and generated content is filtered before it
   reaches the client. Both hold for `POST /notes/regenerate` too.
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
5. **Image occlusion — decided, not generated in v1.** The full implementation is DEC-59, and
   it waits for DEC-58, the app's renderer and region editor for these notes. Until then every
   request that names `image_occlusion` is rejected with `400 VALIDATION_FAILED` (see
   `POST /v1/generations`), and `POST /v1/notes/regenerate` keeps rejecting it as well. It is
   deferred because nothing could use it yet. The app cannot show or edit an image-occlusion
   note, and generated content must be editable before it is saved. Generating it now could not
   be tested end to end, and it would cost a second provider key and a longer job timeout with
   no benefit to users.

   The design DEC-59 implements:

   - **What is hidden.** Each region covers a **text label printed on a labelled diagram**. A
     region carries no answer text, so the answer is whatever the mask hides. Masking a
     structure instead of its label would reveal the structure, not its name.
   - **Stage order is unchanged.** In `generating_cards` the text model (whichever
     `DECKLY_PROVIDER_MODEL_PROVIDER` selects) writes an _occlusion draft_, not a note. A draft
     holds a search phrase for a labelled diagram, the label terms worth hiding, its cited
     sources and an optional `extra`. The generated-content filter screens the draft's text like
     any note. In `fetching_media` the draft's phrase is searched on Commons under the existing
     licence, attribution and image-judge rules. The chosen thumbnail is downloaded and sent to
     the vision provider together with the label terms. The returned boxes are padded, clamped
     to 0–1, merged where they overlap and dropped when too small. The note is then built
     through the same domain validation as every other note: `imageId` is one of its own
     images, regions lie inside the image and ordinals are unique. A draft that does not end as
     a valid note is dropped. The other note types are unaffected.
   - **Vision provider: Gemini, fixed.** It does not depend on `DECKLY_PROVIDER_MODEL_PROVIDER`,
     just as Tavily (search) and Wikimedia Commons (media) do not. Gemini returns boxes natively
     as `[ymin, xmin, ymax, xmax]` normalised to 0–1000, which convert directly to the 0–1
     regions. Claude's coordinates are approximate pixel positions on its own resized copy of
     the image, and DeepSeek has no vision at all. The text-only `LlmPrompt` stays text-only, and
     vision gets its own narrow port, so no text provider needs an "unsupported" branch. A
     server with no vision provider configured keeps rejecting `image_occlusion` with `400`.
   - **The vision call also decides whether the picture is usable.** It returns no regions, and
     the draft is dropped, for an image whose labels are not legible, are numbers keyed to a
     legend, or are not in the card language. This is the check on the pixels themselves that
     the text-only image judge cannot make. Most Commons diagrams are labelled in English, so
     decks in other languages will get few image-occlusion notes.
   - **Contract change.** Each region gains an optional `label`: the term its mask hides.
   - **Failure.** A request for only `image_occlusion` whose drafts all fail ends
     `NO_VALID_CONTENT`, or `PROVIDER_UNAVAILABLE` when an outage of the vision provider caused
     it.
   - **Configuration.** The vision call gets its own `DECKLY_PROVIDER_VISION_*` timeout,
     deadline and circuit breaker. Its deadline joins the sum that must fit within
     `DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS`, which rises to make room.
   - **Regeneration** of an image-occlusion note stays rejected.

   Alternatives rejected:

   - **Regions from metadata.** Image dimensions say nothing about where the labels are.
     Commons' "relative position within image" qualifiers mark depicted objects, not labels,
     and are sparse. Parsing SVG `<text>` elements only works for a fragile subset of files.
   - **Parity across the three text providers.** DeepSeek cannot see images, so the type would
     exist on some deployments and silently not on others.
   - **OCR plus a text model.** This also needs a new provider, and it cannot tell a usable
     diagram from a photo or a numbered legend. It remains the fallback if Gemini's boxes prove
     too loose.

6. **Regenerating a note of a document-sourced deck — rejected in v1 (DEC-83).** The backend keeps
   no deck: `POST /v1/notes/regenerate` carries a topic, not a job id, and answers within 10
   seconds. A document's own download and parse deadlines, part of the document-path job timeout,
   can exceed that budget alone, so re-reading the document inside this call would mean
   re-architecting the endpoint. A web search for a title would instead ground the replacement in
   material that differs from, and may contradict, the rest of the deck. The request therefore
   names the deck's `documentId` in place of a `topic` and is refused with
   `422 DOCUMENT_REGENERATION_UNSUPPORTED` (see `POST /v1/notes/regenerate`). The refusal can only
   rely on the client sending it: a client that sends a topic of its own for a document deck gets
   a web-searched note like any topic deck.

   Deferred, not missing; DEC-83 is where the real design goes if this is revisited. The
   regeneration call would need the document's pages to be readable without a fresh parse, for
   example from the parsed text a job already stored, with the deck's `documentId` as the key.
   Until then the client is expected not to offer regenerate for such decks.
