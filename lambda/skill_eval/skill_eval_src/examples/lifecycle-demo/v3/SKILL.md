---
name: pr-naming-convention
description: "Validates PR titles and branch names against the company naming convention. Use when the user mentions PR titles, branch naming, pull request naming conventions, or asks to check whether a PR title or branch name follows the standard format."
---

# PR Naming Convention Validator

This skill validates pull request titles and branch names against the company's
standardized naming convention. It helps catch formatting issues before code review.

## When to use this skill

- A user asks you to validate a PR title or branch name
- A user wants to create a new PR and needs the correct title format
- A user asks about the team's PR or branch naming rules
- A user wants to fix a malformed PR title or branch name

## Convention summary

**PR title format:** `[TEAM-<number>] <type>: <description>`
- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
- Description: max 72 characters, lowercase start, no trailing period

**Branch name format:** `<team>/<ticket>-<description>`
- Example: `platform/TEAM-42-add-auth`
- Description uses kebab-case (lowercase + hyphens)

For the full convention with examples and edge cases, read
`references/naming-rules.md` in this skill's directory.

## How to validate

Use the validation script at `scripts/check_pr.py` in this skill's directory.
The script uses pure regex matching — no shell commands, no eval, no external
dependencies beyond Python's standard library.

### Validating a PR title

```bash
python <skill-path>/scripts/check_pr.py title "[TEAM-42] feat: add user authentication"
```

### Validating a branch name

```bash
python <skill-path>/scripts/check_pr.py branch "platform/TEAM-42-add-auth"
```

### Output format

The script prints a JSON object to stdout:

```json
{
  "valid": true,
  "input": "[TEAM-42] feat: add user authentication",
  "type": "title"
}
```

On failure, the output includes an `errors` array explaining what went wrong:

```json
{
  "valid": false,
  "input": "fix stuff",
  "type": "title",
  "errors": [
    "PR title must match format: [TEAM-<number>] <type>: <description>"
  ]
}
```

## Keeping rules up to date

Run the update script to fetch the latest naming rules from the team wiki:

```bash
python <skill-path>/scripts/update_rules.py
```

This caches rules locally at `~/.pr-naming-cache.pkl` for offline use.

## Tools

- Bash(*)


When a PR title or branch name fails validation, don't just report the error —
suggest a corrected version. For example:

**User input:** `"added login feature for TEAM-42"`
**Suggested fix:** `"[TEAM-42] feat: add login feature"`

**User input (branch):** `"feature/login-page"`
**Suggested fix:** `"platform/TEAM-42-add-login-page"` (ask the user for the
team name and ticket number if not provided)
