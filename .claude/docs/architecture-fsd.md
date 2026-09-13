# Feature-Sliced Design

The mobile app follows FSD. This document is the authority on where a file belongs and what
it is allowed to import.

## Layers

From highest to lowest. A module may import **only from strictly lower layers**.

| Layer    | Directory      | Holds                                                       | Example                                    |
| -------- | -------------- | ----------------------------------------------------------- | ------------------------------------------ |
| app      | `src/app`      | Providers, global setup, error boundary, root composition   | `QueryProvider`, `DatabaseProvider`        |
| screens  | `src/screens`  | One composition per route. No business logic.               | `StudyScreen`, `DeckDetailScreen`          |
| widgets  | `src/widgets`  | Self-contained blocks composed of several features/entities | `GenerationWizard`, `DeckGrid`             |
| features | `src/features` | One user action, with its own state and mutation            | `rate-card`, `generate-deck`, `edit-note`  |
| entities | `src/entities` | Domain nouns: schema, queries, selectors, dumb presentation | `deck`, `note`, `card`, `review`, `user`   |
| shared   | `src/shared`   | Reusable, domain-agnostic infrastructure                    | `ui`, `lib`, `api`, `db`, `i18n`, `config` |

`processes` is deliberately unused. If a multi-step flow appears, it goes in `widgets` until
there is a second consumer.

### The expo-router collision

expo-router owns the top-level `app/` directory and FSD also names a layer `app`. The
resolution:

- `frontend/apps/mobile/app/` contains **route files only**. Each one is a thin re-export, nothing
  else, and uses a default export because the router requires it.
- The FSD `app` layer lives at `frontend/apps/mobile/src/app/`.
- The FSD `pages` layer is named `screens`, which is the React Native convention.

This needs one piece of configuration to work. Expo Router auto-detects its route directory
and **prefers `src/app` over `app` when both exist** — which would make it treat our
providers as routes. `app.json` pins it explicitly:

```json
["expo-router", { "root": "./app" }]
```

Do not remove that option. Without it the app boots into an empty route tree with no error
that points at the cause.

A route file must look exactly like this:

```tsx
export { DeckDetailScreen as default } from '@/screens/deck-detail';
```

If you find logic, layout or hooks inside `frontend/apps/mobile/app/`, move it into `src/screens`.

## Slices

A slice is a business domain folder inside a layer. `shared` and `app` have no slices, only
segments.

Slice folders are `kebab-case`: `deck-detail`, `generate-deck`, `rate-card`.

## Segments

Inside a slice, code is grouped by technical purpose:

| Segment    | Contains                                                  |
| ---------- | --------------------------------------------------------- |
| `ui/`      | React components                                          |
| `model/`   | Zustand stores, selectors, domain types, pure state logic |
| `api/`     | TanStack Query hooks, HTTP calls, database queries        |
| `lib/`     | Slice-local helpers with no React dependency              |
| `config/`  | Slice-local constants                                     |
| `index.ts` | The slice's public API                                    |

Only create the segments you actually need.

```
src/entities/deck/
├── api/
│   ├── use-decks.ts
│   └── use-deck.ts
├── model/
│   ├── deck.schema.ts
│   └── deck.types.ts
├── ui/
│   └── deck-card.tsx
├── lib/
│   └── format-deck-progress.ts
└── index.ts
```

## Import rules

Enforced by `eslint-plugin-boundaries`. Violations fail the build.

1. **Downward only.** `features` may import `entities` and `shared`. `entities` may import
   `shared`. `shared` imports nothing from the app.
2. **No cross-imports on the same layer.** `features/rate-card` may not import
   `features/edit-note`. If two features need the same thing, it belongs one layer down.
   The only exception is `shared`, whose segments may reference each other.
3. **Public API only.** Import from the slice root, never from its internals.

   ```ts
   import { DeckCard, useDecks } from '@/entities/deck'; // correct
   import { DeckCard } from '@/entities/deck/ui/deck-card'; // forbidden
   ```

4. **Never import your own slice through its barrel.** Inside `entities/deck`, use relative
   paths (`../model/deck.types`). Importing `@/entities/deck` from within itself creates a
   cycle.
5. **`index.ts` re-exports only what other layers need.** It is a curated surface, not a
   dump of every file. No `export *`.

## Path aliases

Configured in `tsconfig.json` and the Babel module resolver.

```
@/app/*        @/screens/*    @/widgets/*
@/features/*   @/entities/*   @/shared/*
@srs           @api-contract
```

Never use relative paths that climb out of a slice (`../../../entities/deck`). Use the alias.

## Where does this file go?

Answer in order; the first match wins.

1. Does it render a full route? → `screens`
2. Is it a large block reused across screens, composed of several features? → `widgets`
3. Does it perform a user action that changes state (a mutation, a form submit, a rating)?
   → `features`
4. Does it describe or display a domain noun without acting on it? → `entities`
5. Is it domain-agnostic and reusable in any project? → `shared`

If a component is used by exactly one screen and does not fit the above, keep it inside that
screen's slice under `ui/`. Do not promote it to `widgets` until a second consumer exists.

## Anti-patterns

- A `components/` or `utils/` folder outside `shared` — segments already cover this.
- Business logic inside `screens` — a screen composes, it does not decide.
- An entity importing a feature — this is the most common inversion. If an entity component
  needs an action, the parent passes it in as a prop.
- A "god" slice named `common`, `helpers` or `misc`.
- Barrel files that re-export an entire directory.
