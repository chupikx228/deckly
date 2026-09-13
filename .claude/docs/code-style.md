# Code style

Normative. Match this exactly; do not introduce personal variations.

## Language

- TypeScript only, `strict: true`. There are no `.js` or `.jsx` files in this repository.
- `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes` and `noImplicitOverride` are on.
  Handle the resulting `undefined` cases, do not silence them.
- **No comments.** If a block needs explanation, the names are wrong or it belongs in a
  markdown doc.

## Typing

- `any` is banned. Use `unknown` at boundaries and narrow with Zod.
- Non-null assertions (`!`) are banned. Narrow explicitly or return early.
- `as` is banned except immediately after a Zod parse, and in `as const`.
- `interface` for object shapes that are extended or implemented, including component props.
  `type` for unions, intersections, mapped and utility types.
- Prefer union literals over enums. Derive the type from the constant object:

  ```ts
  export const NOTE_TYPE = {
    BASIC: 'basic',
    CLOZE: 'cloze',
  } as const;

  export type NoteType = (typeof NOTE_TYPE)[keyof typeof NOTE_TYPE];
  ```

- Every exported function has an explicit return type. Local functions may infer.
- Model impossible states out of existence with discriminated unions instead of optional
  fields that must be checked together.

## Validation

Every value entering the app from outside is parsed with Zod before use. That means HTTP
responses, SQLite rows, MMKV values, deep link params and route params. The Zod schema is
the single definition; the TypeScript type is inferred from it with `z.infer`, never written
twice.

## Files and naming

| Thing                | Convention                                 | Example                             |
| -------------------- | ------------------------------------------ | ----------------------------------- |
| Files and folders    | `kebab-case`                               | `deck-card.tsx`, `use-due-cards.ts` |
| Components           | `PascalCase`                               | `DeckCard`                          |
| Hooks                | `useCamelCase`                             | `useDueCards`                       |
| Stores               | `useXStore`                                | `useStudySessionStore`              |
| Constant objects     | `SCREAMING_SNAKE_CASE`                     | `QUERY_KEY`, `ROUTE`                |
| Types and interfaces | `PascalCase`, no `I` prefix                | `Deck`, `DeckCardProps`             |
| Zod schemas          | `xSchema`                                  | `deckSchema`                        |
| Booleans             | `is` / `has` / `can` prefix                | `isDue`, `hasMedia`                 |
| Event handlers       | `handleX` in the component, `onX` in props | `onPress`, `handlePress`            |

One component per file, and the file is named after it.

## Exports

- Named exports everywhere.
- Default exports appear **only** in `frontend/apps/mobile/app/**` route files, because expo-router
  requires them.

## Components

```tsx
interface DeckCardProps {
  deck: Deck;
  onPress: (deckId: string) => void;
}

export const DeckCard = ({ deck, onPress }: DeckCardProps) => {
  const { t } = useTranslation('deck');

  const handlePress = useCallback(() => {
    onPress(deck.id);
  }, [deck.id, onPress]);

  return (
    <Pressable className="rounded-3xl bg-surface p-4" onPress={handlePress}>
      <Text className="text-lg font-semibold text-foreground">{deck.title}</Text>
      <Text className="text-sm text-muted">{t('card.dueCount', { count: deck.dueCount })}</Text>
    </Pressable>
  );
};
```

Rules:

- Arrow function assigned to a `const`, props destructured in the signature.
- Props interface named `<Component>Props`, declared directly above the component.
- No inline object or array literals in JSX props — they allocate on every render.
- No anonymous functions passed to a child that is memoised.
- Early return for loading and empty states rather than nested ternaries in JSX.
- Order inside a component: hooks, derived values, callbacks, effects, early returns, JSX.

## One component per file, and never nested

This is about extracted components, not markup. Inline JSX inside a `return` is fine — you are
not required to shred a layout into sub-components. The rule fires the moment you pull a piece
out as a `<Child />`: it moves to its own file under `ui/`, named after it, and is imported. A
second component declared in the same file is not allowed.

Never declare a component or a hook inside another component's body. It is recreated on every
render, loses its identity, and the rules of hooks and `no-nested-components` reject it.
Components and hooks live at module top level or in their own file. A parent composes its
children — it holds the structure and passes data and callbacks down as props; the children
are presentational, one file each. Only the slice's public entry point is exported from
`index.ts`; private sub-components are imported by relative path inside the slice.

## When a component is doing too much

Split it — logic into a hook in the slice's `model/`, UI pieces into their own files under
`ui/` — as soon as any one of these is true:

- its JSX runs past roughly 100 lines and you scroll to follow the structure;
- more than four or five `useState` sit together — it is juggling several independent concerns;
- more than two `useEffect` watch different things (a subscription, an animation, an outside
  tap);
- two levels of abstraction share the file — a screen skeleton next to a single control's
  markup;
- business logic and presentation are mixed — the component knows a query key, a validation
  rule and a border colour at once;
- you catch yourself copy-pasting markup or a `handleX` into another file — it belongs in
  `shared`;
- verifying one interaction in a test needs three mocks and a provider;
- a prop is drilled through three levels for one line at the bottom.

Extracting slice logic into a `model/` hook is the default once it starts to crowd the markup,
but it is not a hook per component: if a component only calls a ready query hook and renders,
a `model/` hook is indirection for its own sake. Extract when there is enough logic — state
plus several handlers plus mutations — that it gets in the way of reading the JSX.

## Styling

- NativeWind classNames only. `StyleSheet.create` and inline `style` objects are used only
  where NativeWind cannot express it, such as an animated style driven by Reanimated.
- All colours, spacing, radii and font sizes come from `tailwind.config.ts` tokens. Never a
  raw hex value or a raw pixel number in a component.
- Dark mode is supported by every component from the moment it is written, via `dark:`.
  There is no retrofit pass.
- Spacing follows an 8pt grid. Radii are `rounded-2xl` (16), `rounded-3xl` (24) or
  `rounded-[32px]` for sheets.

## Lists

- `FlashList` for any list that can exceed roughly 20 items. `.map()` only for small static
  collections.
- `keyExtractor` returns a stable domain id, never the index.
- `renderItem` is a hoisted, memoised component, not an inline closure.

## Performance

- Wrap list item components and anything below a frequently updating parent in `memo`.
- `useCallback` for any function passed to a memoised child or into a dependency array.
  Do not wrap functions that are only called locally.
- `useMemo` only for genuinely expensive work or referential stability, not for arithmetic.
- Subscribe to Zustand with a narrow selector. Reading the whole store re-renders on every
  change. See `state-management.md`.
- Animations run on the UI thread via Reanimated worklets. Do not animate with `setState`.

## Async and errors

- `async/await`, never raw `.then()` chains.
- Never swallow an error. Either surface it to the user through a translated message or
  rethrow it so a boundary handles it.
- Errors thrown by the API layer are typed application errors, not raw fetch failures. The
  HTTP client maps status codes to them.
- No `useEffect` for data fetching. That is TanStack Query's job.
- `useEffect` is a last resort in general. Prefer deriving during render, event handlers, or
  a store subscription.

## Testing

- Pure logic in `frontend/packages/srs` is fully unit tested with Vitest. It has no React Native
  dependency and runs in milliseconds.
- Zod schemas covering API responses have a test with a realistic fixture.
- Components are tested with React Native Testing Library only when they contain branching
  logic worth protecting. Snapshot tests are not used.

## Before you finish

1. Types pass with no new errors.
2. No `any`, `!`, unexplained `as`, or comments were introduced.
3. No user-facing string literal was introduced outside a locale file.
4. No new magic string that should have gone into `shared/config`.
5. No unrelated file was modified.
