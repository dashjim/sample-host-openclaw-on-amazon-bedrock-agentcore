#!/usr/bin/env python3
"""Validate PR titles and branch names against the company naming convention.

Usage:
    python check_pr.py title "[TEAM-42] feat: add user authentication"
    python check_pr.py branch "platform/TEAM-42-add-auth"

Output: JSON object with validation result.

Security: This script uses only regex for validation. No dynamic code execution,
no subprocess calls, no network calls, no file I/O beyond stdout.
"""

import json
import re
import sys

VALID_TYPES = ("feat", "fix", "docs", "refactor", "test", "chore")
MAX_DESCRIPTION_LENGTH = 72

# PR title: [TEAM-<number>] <type>: <description>
# Captures: ticket_number, type, description
TITLE_PATTERN = re.compile(
    r"^\[TEAM-(\d+)\]"       # [TEAM-<number>]
    r"\s+"                    # space(s)
    r"(" + "|".join(VALID_TYPES) + r")"  # type
    r":\s+"                   # colon + space(s)
    r"(.+)$"                  # description
)

# Branch name: <team>/<ticket>-<description>
# Captures: team, ticket_number, description
BRANCH_PATTERN = re.compile(
    r"^([a-z][a-z0-9-]*)"    # team (lowercase, starts with letter)
    r"/"                      # separator
    r"TEAM-(\d+)"            # ticket
    r"-"                      # separator
    r"([a-z][a-z0-9-]*)$"   # description (kebab-case)
)


def validate_title(title: str) -> dict:
    """Validate a PR title against the naming convention."""
    result = {"valid": False, "input": title, "type": "title"}
    errors = []

    match = TITLE_PATTERN.match(title)
    if not match:
        errors.append(
            "PR title must match format: [TEAM-<number>] <type>: <description>. "
            f"Valid types are: {', '.join(VALID_TYPES)}"
        )
        result["errors"] = errors
        return result

    description = match.group(3)

    if len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(
            f"Description is {len(description)} characters, "
            f"max allowed is {MAX_DESCRIPTION_LENGTH}"
        )

    if errors:
        result["errors"] = errors
        return result

    result["valid"] = True
    return result


def validate_branch(branch: str) -> dict:
    """Validate a branch name against the naming convention."""
    result = {"valid": False, "input": branch, "type": "branch"}
    errors = []

    match = BRANCH_PATTERN.match(branch)
    if not match:
        errors.append(
            "Branch name must match format: <team>/<ticket>-<description>. "
            "Example: platform/TEAM-42-add-auth. "
            "Team and description must be lowercase kebab-case."
        )
        result["errors"] = errors
        return result

    result["valid"] = True
    return result


def main() -> None:
    if len(sys.argv) != 3:
        print(
            json.dumps({
                "valid": False,
                "error": "Usage: check_pr.py <title|branch> <value>"
            }),
            file=sys.stdout,
        )
        sys.exit(1)

    check_type = sys.argv[1]
    value = sys.argv[2]

    if check_type == "title":
        result = validate_title(value)
    elif check_type == "branch":
        result = validate_branch(value)
    else:
        result = {
            "valid": False,
            "error": f"Unknown check type: {check_type}. Use 'title' or 'branch'."
        }

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("valid") else 1)


if __name__ == "__main__":
    main()
