# Deckly

An Anki-style spaced-repetition app with AI-generated decks. You type a topic — "road signs",
"the 1000 most common English words", "quantum mechanics formulas" — and the backend researches
it into a reviewable deck; the app then schedules reviews on-device with FSRS. Offline-first:
local SQLite owns all user data, and the backend exists only to turn a topic into cards.

<div align="center">

![Expo](https://img.shields.io/badge/-Expo_SDK_54-000020?logo=expo&logoColor=white&style=for-the-badge)
![React Native](https://img.shields.io/badge/-React_Native-61DAFB?logo=react&logoColor=black&style=for-the-badge)
![TypeScript](https://img.shields.io/badge/-TypeScript_strict-3178C6?logo=typescript&logoColor=white&style=for-the-badge)
![Expo Router](https://img.shields.io/badge/-Expo_Router-000020?logo=expo&logoColor=white&style=for-the-badge)
![NativeWind](https://img.shields.io/badge/-NativeWind_v4-06B6D4?logo=tailwindcss&logoColor=white&style=for-the-badge)
![TanStack Query](https://img.shields.io/badge/-TanStack_Query-FF4154?logo=reactquery&logoColor=white&style=for-the-badge)
![Zustand](https://img.shields.io/badge/-Zustand-2D2A26?style=for-the-badge)
![Drizzle](https://img.shields.io/badge/-Drizzle_ORM-C5F74F?logo=drizzle&logoColor=black&style=for-the-badge)
![SQLite](https://img.shields.io/badge/-expo--sqlite-003B57?logo=sqlite&logoColor=white&style=for-the-badge)
![Zod](https://img.shields.io/badge/-Zod-3E67B1?logo=zod&logoColor=white&style=for-the-badge)
![i18next](https://img.shields.io/badge/-i18next-26A69A?logo=i18next&logoColor=white&style=for-the-badge)
![pnpm](https://img.shields.io/badge/-pnpm-F69220?logo=pnpm&logoColor=white&style=for-the-badge)
![Vitest](https://img.shields.io/badge/-Vitest-6E9F18?logo=vitest&logoColor=white&style=for-the-badge)
![ESLint](https://img.shields.io/badge/-ESLint-4B32C3?logo=eslint&logoColor=white&style=for-the-badge)
![Prettier](https://img.shields.io/badge/-Prettier-F7B93E?logo=prettier&logoColor=black&style=for-the-badge)

![Python](https://img.shields.io/badge/-Python_3.12-3776AB?logo=python&logoColor=white&style=for-the-badge)
![FastAPI](https://img.shields.io/badge/-FastAPI-009688?logo=fastapi&logoColor=white&style=for-the-badge)
![Pydantic](https://img.shields.io/badge/-Pydantic_v2-E92063?logo=pydantic&logoColor=white&style=for-the-badge)
![PostgreSQL](https://img.shields.io/badge/-PostgreSQL-4169E1?logo=postgresql&logoColor=white&style=for-the-badge)
![SQLAlchemy](https://img.shields.io/badge/-SQLAlchemy_2.0-D71F00?logo=sqlalchemy&logoColor=white&style=for-the-badge)
![Alembic](https://img.shields.io/badge/-Alembic-6BA81E?style=for-the-badge)
![Redis](https://img.shields.io/badge/-Redis-FF4438?logo=redis&logoColor=white&style=for-the-badge)
![Arq](https://img.shields.io/badge/-Arq-1F6FEB?style=for-the-badge)
![Ruff](https://img.shields.io/badge/-Ruff-D7FF64?logo=ruff&logoColor=black&style=for-the-badge)
![mypy](https://img.shields.io/badge/-mypy_strict-2A6DB2?style=for-the-badge)

</div>

## Structure

```
frontend/             pnpm workspace — everything JavaScript/TypeScript
  apps/mobile/        Expo app — React Native, expo-router, Feature-Sliced Design
  packages/srs/       Scheduler interface + ts-fsrs (FSRS) implementation, pure and offline
  packages/api-contract/  TypeScript types + Zod schemas generated from the OpenAPI spec
backend/              FastAPI + PostgreSQL + Arq — generation service (empty until it starts)
.claude/
  docs/               Frontend guides — FSD, code style, state, i18n, data model
  backend/            API contract + engineering, testing & handoff guides
  skills/             Repo skills (edge-case-bug-hunting)
```

The backend does AI generation only. Local SQLite is the source of truth; there are no
deck/note/card, review, auth or sync endpoints in v1.

## Running

All the JavaScript tooling lives in `frontend/`, so run pnpm from there:

```bash
cd frontend
pnpm install
pnpm --filter @deckly/mobile prebuild   # generates ios/, requires CocoaPods
pnpm ios                                 # expo run:ios, builds a dev client
```

The app depends on native modules (SQLite, MMKV, Reanimated), so Expo Go will not run it — a
dev client build is required. Pinned to **Expo SDK 54** on purpose (see [CLAUDE.md](CLAUDE.md) →
"Toolchain constraints"). Until the real backend exists the app runs against an in-memory mock;
set `EXPO_PUBLIC_API_MOCK=false` and `EXPO_PUBLIC_API_URL` to talk to a real server.

## Before committing

```bash
cd frontend && pnpm run ci    # typecheck + lint (FSD boundaries + strict rules) + format check + tests + OpenAPI lint
```

The same gate runs on a Husky pre-commit hook and in CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
`pnpm format` fixes anything auto-fixable. Never commit to `main` directly, and never push
without asking.

Branches, commits and pull requests — including the Jira `DEC-` key convention — are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Project context

All documentation lives under [`.claude/`](.claude): the product overview, locked decisions and
hard rules in [CLAUDE.md](CLAUDE.md) (which is also the index the AI assistant reads); the
frontend guides in [`.claude/docs/`](.claude/docs); and the backend contract, engineering and
testing guides in [`.claude/backend/`](.claude/backend). A developer picking up the backend
starts at [`.claude/backend/handoff.md`](.claude/backend/handoff.md). Keep these in sync with the
code.
