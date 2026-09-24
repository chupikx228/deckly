# Backend handoff

> **Temporary document — delete it when the handoff is done.** This is scaffolding for the
> person picking up the backend, not a permanent doc. Everything real lives in the files it
> links to; keeping this around after integration only invites drift. See
> [When to delete this file](#when-to-delete-this-file).

The single entry point for building the generation backend in this monorepo. It does not
restate the contract — it points you at the normative files and tells you what is decided and
what is still yours to do.

## Start here — read in this order

1. [`api-contract.md`](api-contract.md) — the contract in prose: endpoints, error codes, note
   field shapes, media rules, quota.
2. [`openapi.yaml`](openapi.yaml) — the machine-readable source of truth. TypeScript types are
   generated from it into `frontend/packages/api-contract`; never hand-edit the generated file.
3. [`engineering-guide.md`](engineering-guide.md) — how to build it: ports & adapters, SOLID,
   the "update the contract first" rule, and the CI gate your service must add.
4. [`testing.md`](testing.md) — how to test: boundaries and failure modes, not the happy path.
5. The mock at [`frontend/apps/mobile/src/shared/api/mock`](../../frontend/apps/mobile/src/shared/api/mock) —
   an **executable reference** for the expected behaviour (stage sequence, timing, idempotency,
   cancellation, the `fail`-topic error path). When in doubt about behaviour, match the mock.

## What is already done

- The contract is complete and **validated** (`pnpm api:lint` is a CI gate).
- Client types are generated from the spec and **compile-time asserted** against it, so a schema
  drift is a build failure on the client.
- The mobile app runs against the mock today (`EXPO_PUBLIC_API_MOCK`), so both sides are
  developed independently and meet on the spec.
- CI scaffolding exists: see [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).

## Decisions already made — do not reopen

- **Scope is locked:** the backend does AI generation only. No deck/note/card CRUD, no review,
  no auth, no sync in v1. Adding any of those is a contract change, not a quiet addition.
- **Topic:** open-domain — any topic; the model generates the cards. Every note must still carry
  at least one source or it is dropped. Content policy still gates (`TOPIC_REJECTED`).
- **Images:** sourced by the model when `includeImages` is set. Every image must carry a real
  `license` and `alt`, or it is dropped — never ship unlicensed media.
- **Quota:** N generation jobs per day per client, already specced in the contract (`Quota` on
  `GenerationJobCreated` and `Health`).
- **No streaming:** the job returns its full result at once; the client shows and edits all cards
  in preview.

The full rationale is in [`api-contract.md`](api-contract.md) → "Resolved decisions".

## What is yours to decide or do first

- **Stack is decided** — Python 3.12, FastAPI, Pydantic v2, PostgreSQL, SQLAlchemy 2.0, Alembic,
  Redis, Arq, Ruff, mypy strict (see the badges in [`README.md`](../../README.md)). The
  engineering guide stays stack-agnostic on purpose — its principles hold regardless — but this
  is the stack to build on. Not an open question.
- **Image licensing source.** "The model finds images" is decided; _how_ the licence is
  established is your call — but it must be derivable, or the image is dropped (the contract
  requires `license`).
- **Decide where the backend code lives** in this monorepo — the README plans on `backend/`.
  The CI placeholder job looks for `backend/`, `apps/backend` or `services/backend` and fails
  once code appears without its own gate.
- **Wire the backend CI gate** per the ruleset in [`engineering-guide.md`](engineering-guide.md)
  → "CI enforcement", replacing that placeholder job.
- **Follow "update the contract first":** any new field, endpoint, note type, stage or error code
  is edited in `openapi.yaml` first, then `pnpm api:generate`, then the docs — in the same change.

## Working in this repo

- An adversarial-QA skill, [`edge-case-bug-hunting`](../skills/edge-case-bug-hunting/SKILL.md),
  is available in this repo and applies to the backend (untrusted model output, job state
  machine, idempotency, rate limiting). [`CLAUDE.md`](../../CLAUDE.md) already asks Claude to run
  it after non-trivial changes.
- The model does **not** guarantee the wire format — you do. Validate the model's output against
  the schema, repair or drop what does not conform, and never pass raw model output through. This
  is the single most common way an AI backend ships bugs.

## When to delete this file

Delete `handoff.md` once all of these are true:

1. All five endpoints are implemented per the contract and validate against `openapi.yaml`.
2. The backend has its own CI gate, and the placeholder job is gone.
3. The mobile app, pointed at the real server with `EXPO_PUBLIC_API_MOCK=false`, runs the full
   generate → poll → preview → import flow end-to-end (the real acceptance test, from
   [`engineering-guide.md`](engineering-guide.md) → "Definition of done").

At that point the handoff is complete and this file is obsolete — its content already lives in
the normative docs it points to. Remove it so nobody reads a stale summary instead of the source.
