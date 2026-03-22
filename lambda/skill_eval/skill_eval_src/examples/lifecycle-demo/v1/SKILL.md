---
name: pr-naming
description: "checks PR names"
---

# PR Name Checker

Checks if PR titles follow our convention.

## How to use

Run the script on a PR title:

```bash
export GITHUB_TOKEN=ghp_test0000000000000000000000000000000000
python3 scripts/check_pr.py "your PR title here"
```

You can also check branch names.

## Rules

- Titles should have a ticket number
- Use standard types like feat, fix, etc

## Tools

- Bash(*)
