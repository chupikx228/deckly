# Working in this repository

Branches, commits and pull requests. The rules are the same for the mobile app and the backend.

## Branches

`main` is always releasable: never push to it directly, only through a pull request. Everything
else is a short-lived branch off `main`.

```
<type>/DEC-<number>/<short-dashed-description>
```

The type is the same as the commit type (below). The Jira key is uppercase, exactly as in Jira.
The description is latin, lowercase, 2–5 words that name the task.

```
feat/DEC-42/generation-preview-edit
fix/DEC-118/study-queue-day-cutoff
refactor/DEC-203/note-field-schemas
docs/DEC-77/backend-testing-guide
```

The key is mandatory: Jira uses it to attach the branch, its commits and the pull request to the
task and to move its status. A branch with no key never shows up on the task, and nobody wires it
up by hand later.

A branch with no Jira task is only for trivia that never enters the tracker (`chore/bump-expo-54`,
`ci/fix-pnpm-cache`). Anything a user sees has a task.

A branch lives until it merges and is deleted right after. There is no long-lived `develop`
branch — the team is small and branching is flat.

## Commits — Conventional Commits

```
<type>(<scope>): <description>

[body — why, if not obvious]

[footer — breaking changes, links, Jira key]
```

The subject is up to ~72 characters, **in English**, imperative mood, no trailing period. The type
and scope are English too — they are part of the spec and the history is built from them.

**Language.** All commit text — subject and body — is English. Same for branch names and PR
titles. Documentation in this repo is English as well.

```
feat(generation): edit a card in the preview before saving

DEC-42
```

The Jira key goes on its own line in the footer, after a blank line. Keep it out of the subject:
the subject is read by people, the footer is read by Jira.

```
feat(study): show the next interval on each rating button
fix(api-contract): accept an optional quota on the health response
refactor(note): move field parsing into per-type Zod schemas
docs(backend): document the boundary-case testing approach
chore(deps): bump expo to 54.0.x
ci: run the OpenAPI lint on pull requests
```

### Types

| Type       | When                                                      |
| ---------- | --------------------------------------------------------- |
| `feat`     | new user-facing functionality                             |
| `fix`      | a bug fix                                                 |
| `refactor` | a code change that does not alter behaviour               |
| `perf`     | a performance improvement                                 |
| `style`    | formatting that does not change meaning                   |
| `docs`     | documentation, including `.claude/`                       |
| `test`     | tests                                                     |
| `build`    | build, dependencies                                       |
| `ci`       | pipelines                                                 |
| `chore`    | anything else that does not belong in the product history |
| `revert`   | reverting a commit                                        |

### Scopes

Frontend: `mobile`, `study`, `generation`, `deck`, `note`, `card`, `ui`, `db`, `i18n`.
Packages: `srs`, `api-contract`.
Backend (once it exists): `backend`, `generation`.
Shared: `deps`, `ci`, `docs`.

The scope is optional when a change is not tied to one place.

### Breaking changes

A `!` after the scope plus a footer:

```
feat(api-contract)!: rename cardCount to targetCardCount

BREAKING CHANGE: the app and the backend must update together; the OpenAPI spec
and the generated types change in the same commit.
```

## Jira (project DEC)

The key is `DEC-<number>`, always uppercase: `DEC-42`, not `dec-42` or `Dec42`. Jira does not
recognise a lowercase key and the link silently fails.

Three places must carry it, each giving a different link:

| Where                                 | Why                                                       |
| ------------------------------------- | --------------------------------------------------------- |
| Branch name                           | Jira shows the branch on the task and offers to open a PR |
| Commit footer                         | commits appear in the task's development panel            |
| PR title — at the end, in parentheses | the PR attaches to the task and moves its status          |

```
feat(generation): edit a card in the preview before saving (DEC-42)
```

You move the status yourself — do not wait for someone to do it at standup:

| When                                   | Status                                      |
| -------------------------------------- | ------------------------------------------- |
| Picked it up, opened a branch          | **In Progress**                             |
| Opened a pull request                  | **In Review**                               |
| PR merged into `main`                  | **Done**                                    |
| Waiting on a reply, another PR, access | **Blocked**, with a comment saying what for |

One task — one branch — one PR. If a task grows, split it into sub-tasks in Jira rather than
dragging three unrelated changes into one branch. If a PR closes several tasks, list every key on
its own footer line, and in the PR title comma-separate them: `(DEC-42, DEC-43)`.

> Status names are the defaults. If the board columns are named differently, adjust the table to
> the real workflow — the `DEC-` key does not change.

## What must ship in one commit

Some files drift apart silently — caught not by tests but only in production. Change them together:

- **Any endpoint, request or response field, note type, job stage or error code** → edit
  [`.claude/backend/openapi.yaml`](.claude/backend/openapi.yaml) first, run `pnpm api:generate`,
  then update [`.claude/backend/api-contract.md`](.claude/backend/api-contract.md) — all in the
  same change. CI fails if the generated types are out of sync with the spec.
- **A new note type** → `frontend/apps/mobile/src/shared/config` note types + its per-type Zod field schema
  in `entities/note` + the backend contract + [`.claude/docs/data-model.md`](.claude/docs/data-model.md).
- **An i18n key** → both `en` and `ru` locale files. A key that exists in only one locale is a bug.
- **A decision on an open question** (retrieval, images, quota, auth) → recorded under `.claude/`,
  not just in chat.

## Pull requests

Run the gate locally before pushing — CI runs exactly this, and failing the pipeline for something
a ten-second local check would have caught is a waste:

```bash
cd frontend && pnpm run ci    # typecheck + lint (FSD boundaries + strict rules) + format check + tests + OpenAPI lint
```

The pnpm workspace lives in `frontend/`; run every pnpm command from there. `pnpm format` fixes
anything auto-fixable.

The PR title is the commit subject plus the Jira key at the end:
`feat(generation): edit a card in the preview before saving (DEC-42)`. In the body: what changes
and why; a before/after screenshot if the UI changed. Put a full link to the task in the body —
`Closes https://<your-jira>.atlassian.net/browse/DEC-42` — the `(DEC-42)` in the title is for
Jira, the full URL in the body is for anyone (or any tool) that pulls task context.

One PR — one task. Refactoring nearby code "while you are here" goes in a separate PR: a mixed diff
cannot be reviewed.

## Git hooks (Husky)

The same gate CI runs is installed locally as a git hook via
[Husky](https://typicode.github.io/husky/), so a failure is caught in seconds on your machine
instead of minutes later in a red pipeline.

**Setup.** One command installs dependencies and enables the hooks — the `prepare` script runs
automatically and installs the hooks into the repo-root `.husky/` (the workspace is in
`frontend/`, `.git` is at the root):

```bash
cd frontend && pnpm install
```

Nothing else is needed from a fresh clone.

**What `pre-commit` runs.** `pnpm run ci` — typecheck, lint (FSD boundaries + strict rules),
format check, tests, and the OpenAPI lint. If it is red, the commit is blocked.

**Bypass.** `git commit --no-verify` (`-n`) skips the hook. CI still runs the same gate on the PR,
so a bypass only defers the failure — use it only for a work-in-progress commit you will fix before
pushing.

## What CI checks

Defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

- **Frontend & packages** — a "no JavaScript in source" guard, `tsc`, ESLint (FSD layer
  boundaries, strict TypeScript and complexity limits, `--max-warnings 0`), a Prettier check, and
  the tests.
- **API contract** — the OpenAPI spec is linted, and the build fails if the generated types have
  drifted from it.
- **Backend** — a placeholder gate today: it passes while there is no backend code and fails the
  moment backend code appears without its own CI, whose ruleset is in
  [`.claude/backend/engineering-guide.md`](.claude/backend/engineering-guide.md).

A red CI is never merged.
