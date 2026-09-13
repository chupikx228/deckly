# Skills — global

Skills are organized by scope. This directory holds **global** skills: cross-cutting ones that
apply to the whole repo, both the frontend app and the backend service.

Claude Code discovers three scopes and offers all of them:

| Scope        | Location                   | Loads                          |
| ------------ | -------------------------- | ------------------------------ |
| **Global**   | `.claude/skills/` (here)   | everywhere                     |
| **Frontend** | `frontend/.claude/skills/` | when working under `frontend/` |
| **Backend**  | `backend/.claude/skills/`  | when working under `backend/`  |

Scoped skills are offered with a path prefix (e.g. `frontend:<name>`), and a scoped skill wins
over a global one of the same name when you are working in that directory (most specific wins).

Put a skill here only when it genuinely applies to both halves. If it targets one side, put it
in that side's scope so it does not surface where it is irrelevant.

## Current global skills

- **[`edge-case-bug-hunting`](edge-case-bug-hunting/SKILL.md)** — adversarial QA: boundary-value
  analysis, destructive/negative testing, pairwise, state-transition and mutation gaps, then
  writes failing regression tests. Cross-cutting: it has both frontend and backend targets, so
  it lives here rather than in either scope.
