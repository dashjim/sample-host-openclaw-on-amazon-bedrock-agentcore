# PR and Branch Naming Convention

This document defines the full naming convention for pull request titles and
branch names across all teams.

## PR Title Format

```
[TEAM-<number>] <type>: <description>
```

### Components

| Component     | Format              | Example                |
|---------------|---------------------|------------------------|
| Ticket        | `[TEAM-<number>]`   | `[TEAM-42]`            |
| Type          | One of the valid types below | `feat`          |
| Description   | Free text, max 72 chars | `add user authentication` |

### Valid Types

| Type       | When to use                                      |
|------------|--------------------------------------------------|
| `feat`     | A new feature or capability                      |
| `fix`      | A bug fix                                        |
| `docs`     | Documentation-only changes                       |
| `refactor` | Code restructuring with no behavior change       |
| `test`     | Adding or updating tests                         |
| `chore`    | Build, CI, dependency updates, or maintenance    |

### PR Title Rules

1. The ticket reference `[TEAM-<number>]` is required and comes first
2. The number must be a positive integer (e.g., `TEAM-1`, `TEAM-999`)
3. Exactly one space between the ticket reference and the type
4. The type must be one of: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
5. A colon and space (`: `) separates the type from the description
6. The description must not exceed 72 characters
7. The description should be a concise summary of the change

### PR Title Examples

**Valid:**
- `[TEAM-42] feat: add user authentication`
- `[TEAM-100] fix: resolve null pointer in payment flow`
- `[TEAM-7] docs: update API reference for v2 endpoints`
- `[TEAM-55] refactor: extract validation logic into shared module`
- `[TEAM-3] test: add integration tests for order service`
- `[TEAM-88] chore: upgrade React to v19`

**Invalid:**
- `feat: add login` — missing ticket reference
- `[TEAM-42] add login` — missing type
- `[TEAM-42] feature: add login` — `feature` is not a valid type (use `feat`)
- `TEAM-42 feat: add login` — ticket must be in square brackets
- `[TEAM-42] feat:add login` — missing space after colon

---

## Branch Name Format

```
<team>/<ticket>-<description>
```

### Components

| Component     | Format                   | Example              |
|---------------|--------------------------|----------------------|
| Team          | Lowercase, kebab-case    | `platform`           |
| Ticket        | `TEAM-<number>`          | `TEAM-42`            |
| Description   | Lowercase kebab-case     | `add-auth`           |

### Branch Name Rules

1. The team segment must start with a lowercase letter
2. The team segment may contain lowercase letters, digits, and hyphens
3. A forward slash (`/`) separates the team from the ticket
4. The ticket follows the `TEAM-<number>` format (uppercase TEAM)
5. A hyphen separates the ticket from the description
6. The description must be lowercase kebab-case (letters, digits, hyphens)
7. The description must start with a lowercase letter

### Branch Name Examples

**Valid:**
- `platform/TEAM-42-add-auth`
- `backend/TEAM-100-fix-payment-null-pointer`
- `frontend/TEAM-7-update-dashboard-layout`
- `data-eng/TEAM-55-migrate-to-spark`

**Invalid:**
- `TEAM-42-add-auth` — missing team prefix
- `platform/add-auth` — missing ticket number
- `platform/TEAM-42` — missing description
- `Platform/TEAM-42-add-auth` — team must be lowercase
- `platform/team-42-add-auth` — ticket `TEAM` must be uppercase
- `platform/TEAM-42-Add-Auth` — description must be lowercase
