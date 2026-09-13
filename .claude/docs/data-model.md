# Data model

Copied deliberately from Anki, because this part of the problem is already solved. Getting
it wrong means rewriting everything from the database up to the study screen.

## Note vs Card

This separation is the single most important decision in the schema.

- A **Note** is the content the user entered: a set of named fields plus a note type.
- A **Card** is one direction of questioning generated from a note by a template.
  **One note produces N cards.**
- A **Review** is one logged rating of one card.

Each card carries its own independent scheduling state. `dog → собака` and `собака → dog`
come from the same note but are learned separately, because recognising a word and producing
it are different skills.

Examples:

| Note type                  | Fields                        | Cards produced |
| -------------------------- | ----------------------------- | -------------- |
| Basic                      | Front, Back                   | 1              |
| Basic (and reversed)       | Front, Back                   | 2              |
| Basic (optional reversed)  | Front, Back, Add Reverse      | 1 or 2         |
| Type-in                    | Front, Back                   | 1              |
| Cloze, 3 deletions         | Text, Extra                   | 3              |
| Multiple choice            | Question, Answer, Distractors | 1              |
| Image occlusion, 5 regions | Image, Regions, Extra         | 5              |

Editing a note's content must not reset the scheduling state of its cards. Adding a cloze
deletion adds a card; removing one removes that card and its reviews.

## Note types

Declared in `shared/config` as a constant object, mirrored by the backend contract.

| Id                        | Behaviour                                                                                                                                                                               | Phase     |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `basic`                   | Front shown, back revealed                                                                                                                                                              | MVP       |
| `basic_reversed`          | Both directions, two cards                                                                                                                                                              | MVP       |
| `basic_optional_reversed` | Reverse card only if the extra field is filled                                                                                                                                          | later     |
| `basic_type_in`           | User types the answer, a character-level diff is shown against the correct one                                                                                                          | MVP+      |
| `cloze`                   | `{{c1::hidden}}` deletions in a text, one card per index                                                                                                                                | MVP       |
| `multiple_choice`         | Question with distractors. Not native to Anki — it exists here because the LLM generates plausible distractors for free, which is exactly the expensive part when writing them by hand. | phase 4   |
| `image_occlusion`         | Cloze over regions of an image. Requires a region editor with gestures and resize handles, which is a self-contained piece of work.                                                     | after MVP |

## Tables

All ids are client-generated UUID strings. All timestamps are epoch milliseconds. Every
table has `createdAt` and `updatedAt`. There are no autoincrement integers anywhere — that
is what allows sync to be added in phase 5 without a migration.

### `decks`

`id`, `title`, `description`, `sourceKind` (`manual` | `generated`), `generationJobId`,
`isArchived`, timestamps.

### `notes`

`id`, `deckId`, `noteType`, `fields` (JSON, validated by a Zod schema chosen per note type),
`tags`, `sources` (JSON array of `{ title, url, retrievedAt }`), timestamps.

`sources` is populated for generated notes and shown in the UI. It is the user's only way to
verify that the model did not hallucinate, so it is not optional for generated content.

### `cards`

`id`, `noteId`, `deckId`, `templateOrdinal`, `state` (`new` | `learning` | `review` |
`relearning`), `due`, `stability`, `difficulty`, `elapsedDays`, `scheduledDays`, `reps`,
`lapses`, `lastReviewedAt`, `isSuspended`, timestamps.

`templateOrdinal` is which card of the note this is — the reverse direction, or the cloze
index. `stability` and `difficulty` are FSRS memory state; see below.

### `reviews`

`id`, `cardId`, `rating` (1 again, 2 hard, 3 good, 4 easy), `state` before the review,
`due` before the review, `stability`, `difficulty`, `elapsedDays`, `lastElapsedDays`,
`scheduledDays`, `reviewedAt`, `durationMs`.

This table is append-only and is never pruned. It is written from day one even though
nothing reads it yet, because FSRS parameter optimisation needs the full history and it
cannot be reconstructed after the fact.

### `media`

`id`, `noteId`, `kind` (`image` | `audio`), `localUri`, `remoteUrl`, `alt`, `width`,
`height`, `license`, timestamps.

Remote media is downloaded and cached locally on first use, so a deck stays reviewable
offline.

### Settings are not a table

`locale`, `contentLanguage`, `theme`, `dailyNewLimit`, `dailyReviewLimit`, `targetRetention`
and `fsrsParameters` live in the Zustand settings store persisted to MMKV, not in SQLite.
They are small key-value preferences with no relations, and keeping them in one place avoids
two sources of truth for the same value.

## Scheduling

FSRS-6, via `ts-fsrs`, wrapped by the `Scheduler` interface in `frontend/packages/srs`.

FSRS tracks three values per card rather than SM-2's single ease factor:

- **Difficulty** — how hard this specific card is for this user.
- **Stability** — how many days until recall probability decays to the target retention.
- **Retrievability** — current probability of recall, decaying continuously along a
  power-law forgetting curve.

A card is due when retrievability falls below the target retention, which defaults to 0.9.
After roughly 1000 reviews the parameters can be optimised against the user's own history
from the `reviews` table.

The scheduler is a pure function:

```ts
interface Scheduler {
  schedule(card: SchedulerCardState, rating: Rating, now: number): SchedulerResult;
  preview(card: SchedulerCardState, now: number): Record<Rating, SchedulerResult>;
}
```

`preview` is what lets the rating buttons show the resulting interval before the user
commits, which is a large part of why Anki feels trustworthy.

No I/O, no React, no database access inside `frontend/packages/srs`. That is what makes it unit
testable in milliseconds and what allows FSRS to be swapped without touching the app.

## Daily queue

Assembled by `features/start-session`:

1. Cards in `learning` or `relearning` whose `due` has passed.
2. Cards in `review` whose `due` has passed, capped by `dailyReviewLimit`.
3. `new` cards, capped by `dailyNewLimit`.

Suspended cards are excluded at every step. The queue is computed once when the session
starts and held in the Zustand session store; it is not recomputed on every render.

"Due" means due before the next **day cutoff**, not before the current instant — a card
scheduled for later today is still available now. The cutoff rolls over at 04:00 local time,
matching Anki, so a late-night session still counts as the previous day. `useDayCutoff` in
`shared/lib` publishes it through `useSyncExternalStore` and re-renders subscribers when the
rollover happens, which keeps `Date.now()` out of render and makes queries react to the day
changing while the app is open.
