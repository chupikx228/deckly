# Skills — backend

Skills scoped to the generation service. Anything here (`backend/.claude/skills/<name>/SKILL.md`)
auto-loads only when working under `backend/`, and Claude Code offers it with a `backend:`
prefix.

Put a skill here when it is specific to the backend — ports & adapters, SOLID/layering checks,
the async job pipeline, provider adapters, prompt/response handling, or anything tied to the
Python stack (FastAPI, Pydantic, SQLAlchemy, Arq). If it applies to the frontend too, use the
repo-root global scope (`.claude/skills/`) instead.

No backend-only skills yet — the backend does not exist yet. The cross-cutting
`edge-case-bug-hunting` skill lives in the global scope and already covers the backend's
boundaries (untrusted model output, job state machine, idempotency, rate limiting).
