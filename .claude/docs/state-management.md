# State management

Three stores exist, and mixing them up is the fastest way to make this app unmaintainable.
Decide which one owns a piece of state before writing it.

## Ownership

| State                                                                         | Owner                                    | Why                                                                             |
| ----------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------- |
| Decks, notes, cards, review log, scheduling state                             | **SQLite**                               | Offline-first. This is the source of truth for everything the user has learned. |
| AI generation jobs, their progress and results                                | **TanStack Query**                       | Lives on the server until the user accepts the result.                          |
| Study session: queue position, flip state, answer input, per-session counters | **Zustand**                              | Ephemeral UI state that dies with the screen.                                   |
| Theme, locale, study settings                                                 | **Zustand + MMKV**                       | Small, persisted, read everywhere.                                              |
| Form field values                                                             | **Local `useState`** or the form library | Never lift a text input into a global store.                                    |

The rule that matters most: **TanStack Query never manages the study session.** The session
is a local queue read out of SQLite. Query talks to the Python backend, nothing else.

## SQLite

- Drizzle ORM. Schema lives in `shared/db/schema`.
- Every table has a `uuid` text primary key generated on the client, plus `createdAt` and
  `updatedAt` as epoch milliseconds. This is what makes sync possible later without a
  migration. Never use autoincrement integers.
- Queries live in the `api/` segment of the owning entity, wrapped in a hook that returns
  typed rows. Rows are parsed with Zod before crossing into domain code.
- Writes go through a mutation in a `features` slice, never directly from a component.
- Reads that must react to writes use Drizzle's live query support so the UI updates without
  manual invalidation.

## TanStack Query

Used exclusively for the generation API.

### Query keys

Never write a key array inline. Every key comes from
`frontend/apps/mobile/src/shared/config/query-keys.ts`.

```ts
import { queryKeys } from '@/shared/config';

useQuery({
  queryKey: queryKeys.generation.job(jobId),
  queryFn: () => fetchGenerationJob(jobId),
});
```

Key structure is hierarchical so that a broad invalidation cascades:

```
[GENERATION]                         invalidates everything generation-related
[GENERATION, 'job', jobId]           one job
```

### Defaults

Set once on the `QueryClient` in `src/app/providers`:

- `staleTime` 60 seconds for anything but job polling.
- `retry` 2 with exponential backoff; never retry a 4xx.
- `refetchOnWindowFocus` false — it is not a web app.

### Polling a generation job

Generation takes 30–120 seconds, so it is an asynchronous job. Poll with a dynamic interval
and stop as soon as the job reaches a terminal state:

```ts
useQuery({
  queryKey: queryKeys.generation.job(jobId),
  queryFn: () => fetchGenerationJob(jobId),
  refetchInterval: (query) =>
    isTerminalJobStatus(query.state.data?.status) ? false : POLL_INTERVAL_MS,
});
```

Never poll forever. A job that has not progressed past the deadline surfaces a translated
timeout error with a retry action.

### Mutations

- Optimistic updates only where a rollback is genuinely cheap. Deck creation is not one of
  those places.
- `POST /generations` carries an `Idempotency-Key`, so a retry after a network drop does not
  bill the user twice.
- On success, invalidate the narrowest key that covers the change.

## Zustand

- One store per concern, in the `model/` segment of the slice that owns it.
- **Always select narrowly.** Reading the store object re-renders the component on every
  unrelated change:

  ```ts
  const currentCard = useStudySessionStore((state) => state.currentCard); // correct
  const { currentCard } = useStudySessionStore(); // forbidden
  ```

- Select multiple fields with `useShallow`, not by returning a new object literal.
- Actions live in the store next to the state they mutate. Components call actions; they do
  not call `set` themselves.
- Never store derived state. Compute it in a selector.
- **Never mirror server or SQLite state into a store "for speed".** There is one source of
  truth per piece of state (see the ownership table). A copy in Zustand drifts from it and you
  end up reconciling two stores.
- **Keep genuinely local state next to the widget or screen that owns it** — a collapsed
  panel, a scroll position, an unsent text draft — with `useState`, not the session store.
  Promote state into a store only when more than one slice must see a consistent view of it.
- Persistence uses the `persist` middleware backed by MMKV, and only for the settings store.
  The study session must not survive an app restart.

## Study session flow

1. `features/start-session` reads due cards from SQLite through `entities/card`, applies the
   daily limit, and hydrates the Zustand session store.
2. The store holds the queue, the index, the flip state and session counters. It never
   touches the network.
3. `features/rate-card` takes the user's rating, calls `frontend/packages/srs` to compute the next
   interval, writes the updated card state and a new `reviews` row to SQLite, and advances
   the queue index.
4. When the queue empties, the session store produces a summary and the screen navigates to
   the summary route. The store resets on unmount.

The scheduler is pure: it takes the card's FSRS state plus a rating and returns the new
state. It performs no I/O, which is what makes it testable in isolation.
