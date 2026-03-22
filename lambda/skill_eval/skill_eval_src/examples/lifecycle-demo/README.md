# Lifecycle Demo: PR Naming Convention Skill

This example demonstrates the full skill evaluation lifecycle using a real-world
scenario: a company-internal PR naming convention validator.

## Why This Skill?

Every company has its own PR/branch naming rules that AI models don't know about.
This makes it an ideal skill — it provides **domain-specific knowledge** that
genuinely improves agent performance.

## Three Versions

| Version | What Happened | Audit Score | Grade |
|---------|-------------|------------|-------|
| `v1/` | User wrote it themselves | 39 | F |
| `v2/` | Used Anthropic Skill Creator | 98 | A |
| `v3/` | Someone added a "feature" | 61 | D |

### v1 — The First Attempt

Problems: hardcoded GitHub token, `eval()` for regex, `subprocess.run(shell=True)`,
vague description, `Bash(*)` permissions.

### v2 — After Skill Creator

Clean implementation: pure regex, no security issues, detailed SKILL.md with trigger
phrases, `references/naming-rules.md` documentation. Generated using Claude's
Skill Creator.

### v3 — Regression

Someone added `scripts/update_rules.py` to "auto-update rules from the wiki."
Introduced `pickle.load()` (arbitrary code execution) and `shell=True` back.

## Running the Demo

```bash
# Compare audit scores across versions
skill-eval audit examples/lifecycle-demo/v1/
skill-eval audit examples/lifecycle-demo/v2/
skill-eval audit examples/lifecycle-demo/v3/

# Run functional eval (requires Claude CLI)
skill-eval functional examples/lifecycle-demo/v2/ --runs 1

# Run trigger eval
skill-eval trigger examples/lifecycle-demo/v2/ --runs 1

# Save v2 as baseline and detect v3 regression
skill-eval snapshot save examples/lifecycle-demo/v2/ --tag "after-skill-creator"
skill-eval compare examples/lifecycle-demo/v2/ examples/lifecycle-demo/v3/
```

## Ground Truth

See `ground-truth.md` for the full expected results and verification methodology.
