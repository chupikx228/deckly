# CLAUDE.md

Instructions for Claude working in this repository. Read this file first, then the
detailed guides in `.claude/docs/` before touching any code.

## Product

An Anki-style spaced repetition app with AI-generated decks. The user types a topic
("road signs", "1000 most common English words", "quantum mechanics formulas") and the
backend researches, parses and turns it into a reviewable deck. The app then schedules
reviews locally with FSRS.

Two halves, with different risk profiles:

- **SRS core** — a solved problem. Copy Anki's proven model faithfully. Must work offline.
- **AI generation** — the reason the product exists. All generated content is reviewable
  and editable by the user before it is saved.

## Locked architectural decisions

Do not revisit these without an explicit instruction from the user.

| Decision         | Choice                                                                                 | Consequence                                                                                              |
| ---------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Data ownership   | Offline-first. Local SQLite is the source of truth.                                    | The backend is used **only** for AI generation. No deck CRUD endpoints.                                  |
| Sync             | None in v1, planned for phase 5.                                                       | Every table carries a `uuid` primary key and `updatedAt`, so sync can be added without a migration.      |
| Scheduler        | FSRS via `ts-fsrs`, wrapped behind a `Scheduler` interface in `frontend/packages/srs`. | Scheduling runs entirely on the device. The `reviews` table is written from day one.                     |
| Auth             | None in v1. Local anonymous user.                                                      | The `entities/user` slice exists with a local-only implementation. Profile tab shows stats and settings. |
| Backend contract | OpenAPI-first. Spec lives in `.claude/backend/`.                                       | The mobile app is developed against mocks that implement the spec.                                       |
| i18n             | `i18next` from day one, `en` + `ru`.                                                   | **No user-facing literal strings in components, ever.**                                                  |

## Stack

| Concern           | Tool                                                 |
| ----------------- | ---------------------------------------------------- |
| Package manager   | pnpm workspaces (`node-linker=hoisted` is mandatory) |
| Runtime           | Expo with prebuild, CocoaPods, dev client            |
| Language          | TypeScript, `strict: true`. Never JavaScript.        |
| Routing           | expo-router (typed routes)                           |
| Styling           | NativeWind v4, tokens in `tailwind.config.ts`        |
| Server state      | TanStack Query                                       |
| Client state      | Zustand                                              |
| Local database    | expo-sqlite + Drizzle ORM                            |
| Key-value storage | MMKV                                                 |
| Animation         | Reanimated 3 + Gesture Handler                       |
| Validation        | Zod at every external boundary                       |
| i18n              | i18next + react-i18next + expo-localization          |

## Repository layout

```
.
├── CLAUDE.md
├── .claude/
│   ├── docs/                  Architecture and style guides
│   └── backend/               API contract for the Python backend
├── frontend/                  pnpm workspace — all JS/TS tooling and code
│   ├── apps/
│   │   └── mobile/            Expo application
│   └── packages/
│       ├── srs/               Scheduler interface + ts-fsrs implementation
│       └── api-contract/      TypeScript types generated from the OpenAPI spec
└── backend/                   Python generation service (FastAPI, see backend/README.md)
```

## Toolchain constraints

The project is pinned to **Expo SDK 54** on purpose. SDK 56 and later require Xcode 26.4+
(Swift 6.3) to compile `ExpoModulesJSI`; this machine runs Xcode 16.2, where the build fails
during package resolution with `package 'apple' is using Swift tools version 6.2.0 but the
installed version is 6.0.0`. SDK 54 needs only Xcode 16.1+.

Do not run `expo install --fix` against a newer SDK, and do not bump `expo` past `~54` until
the local Xcode is upgraded. If you do, the JavaScript side will keep working and only the
native build will break, which makes the cause easy to misread.

Two related pins exist for the same reason:

- `@expo/vector-icons` is forced to `15.0.3` by an override in `pnpm-workspace.yaml`.
  Without it, pnpm resolves a newer copy that drags in `expo-font@57`, and the native build
  ends up with two versions of the same module.
- `expo-image` is **not** listed under `plugins` in `app.json`. It has no config plugin in
  SDK 54, and listing it makes `expo config` fail outright.

## Running the app

```bash
cd frontend
pnpm install
pnpm --filter @deckly/mobile prebuild   # generates ios/, requires CocoaPods
pnpm ios                                 # expo run:ios, builds a dev client
```

The pnpm workspace lives in `frontend/`; run every pnpm command from there. `pnpm typecheck`,
`pnpm lint` and `pnpm test` run across every workspace package. The whole app depends on native
modules (SQLite, MMKV, Reanimated), so Expo Go will not run it — a dev client build is required.

Metro resolution in this monorepo depends on two settings that must not be removed:
`nodeLinker: hoisted` in `pnpm-workspace.yaml`, and the `watchFolders` / `nodeModulesPaths`
entries in `frontend/apps/mobile/metro.config.js`.

## Generation backend, mocked

The Python backend does not exist yet, so `shared/api` picks its transport at startup:

```
EXPO_PUBLIC_API_MOCK=false   # talk to the real backend at EXPO_PUBLIC_API_URL
```

Anything else (including unset) uses the in-memory mock in `shared/api/mock`, which
implements the same four endpoints, walks through the real stage sequence over about eight
seconds, and honours idempotency keys and cancellation. A topic containing the word `fail`
makes the job fail, which is how the error path is exercised.

Both transports satisfy the same `GenerationTransport` interface and both parse responses
with the Zod schemas from `@deckly/api-contract`, so swapping them cannot change the shape
the app sees.

## Detailed guides

Read the guide that covers the area you are about to change. They are normative, not
suggestions.

- [`.claude/docs/architecture-fsd.md`](.claude/docs/architecture-fsd.md) — layers, slices,
  segments, import rules, where a new file belongs
- [`.claude/docs/code-style.md`](.claude/docs/code-style.md) — TypeScript, React, naming,
  file conventions, performance rules
- [`.claude/docs/state-management.md`](.claude/docs/state-management.md) — TanStack Query
  vs Zustand vs SQLite, query keys, mutation patterns
- [`.claude/docs/i18n.md`](.claude/docs/i18n.md) — locales, namespaces, key naming,
  typed translations, formatting
- [`.claude/docs/data-model.md`](.claude/docs/data-model.md) — note/card model, note types,
  review log, FSRS state
- [`.claude/backend/api-contract.md`](.claude/backend/api-contract.md) — endpoint contract
  the Python backend must implement
- [`.claude/backend/openapi.yaml`](.claude/backend/openapi.yaml) — the machine-readable spec

## Hard rules

These override any default behaviour.

1. **No comments in source code.** The code and its names carry the meaning. Markdown docs
   are the place for explanation.
2. **TypeScript only.** No `.js` or `.jsx` files anywhere, including config files where a
   TypeScript equivalent exists.
3. **No `any`.** No non-null assertions (`!`). No `as` casts except immediately after a Zod
   parse at a system boundary.
4. **No magic strings.** Query keys, routes, screen ids, storage keys and locale codes come
   from `frontend/apps/mobile/src/shared/config`. If you are about to type a string literal that
   appears in more than one place, add it there instead.
5. **No user-facing string literals.** Every string a human reads goes through `t()`.
6. **FSD import direction is enforced.** A layer may only import from strictly lower layers,
   always through a slice's public API. ESLint fails the build otherwise.
7. **Smallest possible change.** Do not refactor unrelated code, rename files, or introduce
   a new pattern when an existing one fits.
8. **No new dependencies** without asking. Search the repo for an existing utility, hook or
   component before writing a new one.
9. **Never run git commands that write.** No commits, no pushes, no branches, no PRs.

## Workflow expectations

Before implementing, estimate the size:

- **Small** (1–2 files, no architectural impact) — just do it.
- **Medium** (3–10 files) — present the plan first.
- **Large** (10+ files, or any change to architecture, state management, or a public
  contract) — present current state, proposed solution, affected files and risks, then wait
  for approval.

After implementing, verify: types pass, no duplicated logic, no unnecessary re-renders, and
no unrelated files were modified.

When the change adds or alters real behaviour — a new endpoint, a validation rule, a state
machine, a store action, a form, a parser, anything with branches — run the
[`edge-case-bug-hunting`](.claude/skills/edge-case-bug-hunting/SKILL.md) skill against what you
just touched before treating the work as done. Derive the boundary and failure cases for the
change, not the happy path, and either fix what it surfaces or report it. This applies to both
the mobile app and the backend. Skip it only for changes with no logic: docs, formatting,
renames, pure config.
