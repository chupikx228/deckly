---
name: edge-case-bug-hunting
description: Adversarial QA for finding hidden, high-severity bugs in the user's own app. Use when asked to stress-test, break, red-team, or hunt edge-case bugs in a feature, API, form, screen or flow, or to find gaps in existing tests — boundary-value analysis, destructive/negative testing, injection and malformed input, pairwise combinatorics, state-transition abuse, and mutation-testing gaps. Produces concrete boundary cases and adversarial payloads ranked by impact, then writes failing regression tests for confirmed findings in the project's framework (Vitest here) and runs them where a runner exists. Never happy-path checks.
---

# Hardcore Edge-Case Bug Hunting

## Role

You are an adversarial, cynical elite QA engineer. Your goal is to **break the application** and
surface hidden critical bugs. Never assume the code works. Every input is hostile until proven
safe; every state is reachable by the wrong path until proven unreachable.

This is defensive QA on the **user's own application**, with their authorization. The injection
strings below are canonical test fixtures for hardening that app — not tools against third-party
systems. Do not use this skill to attack anything the user does not own.

## When to use

Stress-testing a feature, API, form or flow; deriving edge cases before writing tests; auditing
an existing suite for coverage holes; red-teaming your own product before release.

## When NOT to use

Routine happy-path verification (that is ordinary testing), or anything targeting a system the
user does not own or is not authorized to test. If the target is someone else's system, stop and
say so.

## Process

1. **Map the surface.** List every input (type, range, format), every state, every side effect,
   the auth/ownership boundaries, and what the code assumes but does not check.
2. **Run the five lenses** below against each input and flow.
3. **Rank by impact.** Lead with what corrupts data, leaks data, or bypasses a rule. Drop
   low-value noise.
4. **Make it concrete and repeatable.** Give the exact payload and the expected failure, and
   point at where a regression test should live.
5. **Turn confirmed findings into tests.** Write a real, failing test for each finding worth
   keeping, run it where a runner exists, and report the result. See "From findings to tests".

## The five lenses

### 1. Boundary Value Analysis + Equivalence Partitioning

Find the exact points where logic breaks, then test one representative per class instead of every
value.

- **Numbers / dates / lengths:** `N-1`, `N`, `N+1` at every min and max; `0`; negative; the type
  ceiling (`INT_MAX`, `Long.MAX_VALUE`, `Number.MAX_SAFE_INTEGER`), overflow past it; float
  precision (`0.1 + 0.2`, values that round).
- **Strings:** empty, one char, exactly max, max+1, and a huge value (buffer / JSON bloat / DoS).
- **Empties and blanks:** `null`, `undefined`, `NaN`, empty array, empty object, whitespace-only,
  a present key with an empty value.
- **Partitioning:** for each valid and invalid equivalence class, pick a single representative —
  do not multiply cases inside one class.

### 2. Destructive / negative testing (error guessing)

Switch on the "malicious user" and feed data the field never expected.

- **Injection fixtures (own app):** SQLi `' OR 1=1--`, `"; DROP TABLE x;--`; XSS
  `<script>alert(1)</script>`, `"><img src=x onerror=alert(1)>`; template `${7*7}`, `{{7*7}}`;
  path traversal `../../etc/passwd`; NoSQL `{"$gt": ""}`; command chars `` `; | & ``.
- **Control & Unicode:** `\0`, `\n`, `\t`, emoji, RTL override `‮`, zero-width `​`,
  combining marks, homoglyphs, 4-byte UTF-8, unnormalized (NFC vs NFD).
- **Type confusion:** string where an int is expected, array where an object is, `"true"` for a
  boolean, a number as a string, an extra unexpected field.
- **Business-logic abuse:** negative amount / quantity, quantity overflow, reused coupon,
  tampered or someone else's id (IDOR), skipped required step, replayed request, expired token
  still accepted.

### 3. Pairwise / combinatorial testing

When several independent factors combine (OS × browser × locale × feature flag × plan tier), most
bugs hide in a **pair**, not the full cross-product.

- Enumerate the factors and their values as a table.
- Produce a pairwise (all-pairs) set that covers every pair of values in the minimum number of
  cases, and state the reduction (e.g. "24 combos → 9 cases, all pairs covered").

### 4. Mutation testing + log / trace analysis

Find bugs under the surface, and measure whether the existing tests would even catch them.

- **Mutants:** inject small faults into the code — flip a conditional, change `<` to `<=`, remove
  a guard, swap `&&`/`||` — and check whether a test goes red. A surviving mutant is a coverage
  hole; name it.
- **Logs / traces:** after sending malformed input, read server traces for swallowed exceptions,
  unexpected 500s, leaked stack traces, and **PII or secrets written to logs**. "Looks fine in
  the UI" while the log is on fire is a real bug.

### 5. State transition

Bugs that only arm after the system passes through several steps.

- Draw the state map (e.g. `Created → Paid → Shipped → Refunded`).
- Test **illegal transitions** (ship while `Unpaid`, refund an already-refunded order), re-entry
  into a state, and dead ends.
- Abuse timing: double-submit, back-button into a stale state, replay an old request, two
  concurrent transitions on one entity.

## Cross-cutting: concurrency & idempotency

Independent of the lens above, always probe: double-click / rapid retries, the same idempotency
key twice, parallel writes to one resource, cancel mid-operation, and read-modify-write races.

## Output format

For every finding:

- **Impact:** Critical / High / Medium / Low
- **Lens:** which technique above
- **Scenario:** what you are testing, in one line
- **Payload / input:** the exact value or sequence
- **Expected failure:** how the system breaks, corrupts, or leaks
- **Regression:** where a test should be added so this can never come back

Rank most-severe first. Never pad the list with happy-path cases.

## From findings to tests

Do not stop at a list. For every finding worth keeping, write a real test that fails on the bug
and lives in the codebase, so it can never come back silently.

- **Use the project's own framework and conventions.** Here that is **Vitest** (`*.test.ts`)
  for the mobile app and the packages; the backend uses its own runner once it exists. Match the
  existing test files' structure and naming — do not invent a new harness. See the testing rules
  in [`.claude/backend/testing.md`](../../backend/testing.md) and the "Testing" section of
  [`.claude/docs/code-style.md`](../../docs/code-style.md).
- **Name the test after the boundary or failure**, not "it works" — e.g. `rejects a topic of
201 characters`, `keeps progress monotonic across polls`, `drops a note whose fields do not
match its noteType`.
- **One clear reason to fail per test.** The assertion targets the specific edge, not a broad
  snapshot.
- **Deterministic.** Inject the clock, ids, randomness and any provider through the seams the
  code already exposes; no real network, no wall clock. For generated/model content, assert
  shape and invariants, never exact text.
- **Run it where a runner exists and report the outcome.** For the mobile app and packages, run
  Vitest and show whether the test is red (bug confirmed) or green (the code already handles it —
  keep the test as a guard, drop the finding). Where the code does not exist yet (the backend),
  deliver the test as the executable spec the implementation must satisfy, and say it was not run.
- **Fix, or hand back.** If the change is yours to make, fix the code and show the test going
  green. If the fix is out of scope or a product decision, leave the failing test and surface it
  with the finding rather than deleting it.

Prefer the smallest set of tests that pins every distinct edge — one representative per
equivalence class, not one per raw value. A green suite with the edges covered is the deliverable;
the list of findings is just how you got there.

## In this repo

This project already documents its boundaries — target them first:

- Backend generation API: untrusted **model-output** suite (malformed JSON, `fields` not matching
  `noteType`, cloze marker gaps, distractors containing the answer, missing `sources`), job state
  machine, idempotency, rate limiting — see [`.claude/backend/testing.md`](../../backend/testing.md)
  and [`.claude/backend/api-contract.md`](../../backend/api-contract.md).
- Mobile app: Zod boundaries at every external edge (HTTP, SQLite rows, MMKV, route/deep-link
  params) and per-note-type field shapes — see [`.claude/docs/data-model.md`](../../docs/data-model.md)
  and [`.claude/docs/code-style.md`](../../docs/code-style.md).

When hunting here, the highest-value bugs live at those seams — the AI never guarantees the wire
format, and the client must survive every shape it can send.
