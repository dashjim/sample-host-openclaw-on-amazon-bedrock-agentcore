# Meta-Evaluation Results

**Question: Is skill-eval accurate? Can we trust its evaluations?**

We tested skill-eval against 3 target skills with known ground truth, across all three
evaluation dimensions (audit, functional, trigger).

## Target Skills

| Skill | Type | Audit Ground Truth |
|-------|------|-------------------|
| `examples/data-analysis/` | Good skill, deterministic CSV analysis script | 98/A (1 info) |
| `examples/golden-dataset/bad-skills/sloppy-weather/` | Bad skill, hardcoded API key, bash(*) | 53/F (1 crit, 2 warn, 1 info) |
| `tests/fixtures/good-skill/` | Clean minimal skill | 100/A (0 findings) |

## 1. Audit Accuracy ✅ (Deterministic — 100% Verifiable)

Audit is purely deterministic (regex, AST, YAML parsing). No LLM involved.

| Skill | Expected Score | Actual Score | Match? |
|-------|---------------|-------------|--------|
| data-analysis | 98/A | 98/A | ✅ |
| sloppy-weather | 53/F | 53/F | ✅ |
| good-skill | 100/A | 100/A | ✅ |

**Finding-level accuracy:** 36 golden dataset tests verify individual findings
(SEC-001 detected, PERM-001 flagged, etc.). All pass. **Audit accuracy: 100%.**

## 2. Functional Eval Accuracy

Functional eval measures whether an agent performs better WITH the skill than WITHOUT.
This tests assertion grading accuracy + delta computation.

### data-analysis (6 eval cases)

| Case | With Skill | Without Skill | Delta | Notes |
|------|-----------|--------------|-------|-------|
| summary-stats | 100% | 100% | 0% | Both agents read CSV correctly |
| json-output | 60% | 60% | 0% | Both wrap JSON in markdown backticks (fails `starts with '{'`) |
| top-products | 100% | 100% | 0% | Both compute correctly |
| anomaly-detection | 100% | 100% | 0% | Both identify $15,000 outlier, OR assertions work ✅ |
| region-breakdown | 100% | 100% | 0% | Both produce correct table |
| script-execution | 67% | 100% | -33% | LLM judge inconsistency |
| **Overall** | **87.8%** | **93.3%** | **-5.6%** | |

**Analysis:** Delta ≈ 0 because data-analysis has a deterministic script that any agent
can run. The skill provides knowledge (SKILL.md), but the script is self-documenting.
The -5.6% is noise from LLM judge inconsistency on `script-execution`.

### sloppy-weather (2 eval cases)

| Case | With Skill | Without Skill | Delta | Notes |
|------|-----------|--------------|-------|-------|
| weather-basic | 100% | 100% | 0% | Both mention NYC + weather |
| weather-api-key-concern | 100% | 0% | **+100%** | **Only with-skill can see the SKILL.md to find the API key!** |
| **Overall** | **100%** | **50%** | **+50%** | |

**Analysis:** This is the cleanest proof of skill value. The `weather-api-key-concern`
case asks the agent to review the skill for security issues. Without the skill injected,
the agent literally cannot see the SKILL.md — it reports "working directory is empty."
With the skill, it correctly identifies the hardcoded API key as a critical security risk.

**This validates that functional eval correctly measures skill impact when there's
a genuine capability difference.**

### good-skill (2 eval cases)

| Case | With Skill | Without Skill | Delta | Notes |
|------|-----------|--------------|-------|-------|
| process-input | 33% | 33% | 0% | Agent creates own script instead of using existing one |
| describe-skill | 100% | 0% | **+100%** | **Only with-skill can read the SKILL.md metadata!** |
| **Overall** | **66.7%** | **16.7%** | **+50%** | |

**Analysis:** Same pattern as sloppy-weather. Tasks requiring SKILL.md knowledge show
+100% delta. Tasks where the agent can figure it out independently show 0%.

### Functional Accuracy Summary

| Metric | data-analysis | sloppy-weather | good-skill |
|--------|-------------|---------------|-----------|
| With skill pass rate | 87.8% | 100% | 66.7% |
| Without skill pass rate | 93.3% | 50% | 16.7% |
| Delta | -5.6% | **+50%** | **+50%** |
| Deterministic assertions correct? | ✅ | ✅ | ✅ |
| OR assertions working? | ✅ | ✅ | ✅ |
| LLM judge consistent? | ⚠️ 1 inconsistency | N/A (no LLM assertions) | N/A |

**Key findings:**
1. **Deterministic assertion grading is 100% accurate** — every `contains`, `starts with`,
   `matches regex`, and OR compound assertion produced the correct result
2. **LLM judge has minor inconsistencies** — same assertion graded differently for
   with-skill vs without-skill on `script-execution` case (confidence 0.82 vs 0.50)
3. **Functional eval correctly identifies skill value** when tasks require skill-specific
   knowledge (+100% delta on SKILL.md-dependent tasks)
4. **Functional eval correctly shows no difference** when agents can independently solve
   tasks (0% delta on general capability tasks)

## 3. Trigger Eval Accuracy

Trigger eval checks whether the skill activates for relevant queries.

### Before CLI detection fix (v1 — parser only handled API format)

| Skill | Positive (should trigger) | Negative (should NOT trigger) |
|-------|--------------------------|------------------------------|
| data-analysis | 0/5 detected ❌ | 5/5 correct ✅ |
| sloppy-weather | 0/3 detected ❌ | 3/3 correct ✅ |
| good-skill | 0/2 detected ❌ | 3/3 correct ✅ |
| **Overall** | **0/10 (0% recall)** | **11/11 (100% specificity)** |

### After CLI detection fix (v2 — Claude CLI format + Bash command detection)

| Skill | Positive (should trigger) | Negative (should NOT trigger) |
|-------|--------------------------|------------------------------|
| data-analysis | 3/5 detected ✅ | 4/5 correct (1 false positive) |
| sloppy-weather | **3/3 detected** ✅ | **3/3 correct** ✅ |
| good-skill | 0/2 detected ❌ | 3/3 correct ✅ |
| **Overall** | **6/10 (60% recall)** | **10/11 (91% specificity)** |

**Improvements:**
- **sloppy-weather: 0% → 100%** — perfect score after fix, all weather queries
  correctly detected as triggering the weather.py script
- **data-analysis: 0% → 60%** — 3/5 queries correctly detected (agent ran
  analyze_csv.py via Bash). 2 queries didn't trigger because agent answered
  from general knowledge without running scripts
- **good-skill: unchanged at 0%** — agent never ran process.py for the generic
  queries. This reflects real behavior, not a detection bug
- **1 false positive** on data-analysis: "Deploy my application to production"
  triggered because the agent used Bash in a way that matched script detection

**Root cause of the fix:** The parser was only handling Anthropic raw API format
(`content_block_start`), but Claude CLI uses `{type:"assistant", message:{content:[...]}}`
format. After fixing the parser AND adding Bash command analysis, trigger detection
improved dramatically.

## Overall Assessment

| Dimension | Accuracy | Confidence |
|-----------|----------|-----------|
| **Audit** | 100% | High — deterministic, fully verifiable |
| **Functional (deterministic assertions)** | 100% | High — all correct |
| **Functional (LLM assertions)** | ~90% | Medium — occasional judge inconsistency |
| **Functional (delta computation)** | Correct | High — accurately reflects skill impact |
| **Trigger (recall)** | 60% (was 0%) | Medium — CLI detection working, some queries don't use tools |
| **Trigger (specificity)** | 91% (was 100%) | High — 1 false positive out of 11 negatives |

## Recommendations

1. **Audit: Production-ready** — 100% accuracy, fully deterministic
2. **Functional: Production-ready for deterministic assertions** — prefer `contains`,
   `matches regex`, `starts with` over LLM-judged assertions for reliable grading
3. **Trigger: Significantly improved** — CLI detection working; remaining gaps are
   when agents answer from general knowledge without invoking skill tools
4. **LLM judge: Use cautiously** — set a higher confidence threshold (e.g., 0.85)
   to flag uncertain judgments rather than auto-pass at 0.50
5. **False positive mitigation** — tighten Bash command matching to require more
   specific script path references rather than partial name matches

## How to Reproduce

```bash
# Audit (no Claude CLI needed)
skill-eval audit examples/data-analysis/ --format json
skill-eval audit examples/golden-dataset/bad-skills/sloppy-weather/ --format json
skill-eval audit tests/fixtures/good-skill/ --format json

# Functional (requires Claude CLI + API access, ~$5-10 per full run)
skill-eval functional examples/data-analysis/ --runs 1 --timeout 180 --format json
skill-eval functional examples/golden-dataset/bad-skills/sloppy-weather/ --runs 1 --timeout 180 --format json
skill-eval functional tests/fixtures/good-skill/ --runs 1 --timeout 180 --format json

# Trigger (requires Claude CLI)
skill-eval trigger examples/data-analysis/ --runs 1 --timeout 60 --format json
skill-eval trigger examples/golden-dataset/bad-skills/sloppy-weather/ --runs 1 --timeout 60 --format json
skill-eval trigger tests/fixtures/good-skill/ --runs 1 --timeout 60 --format json
```

## Ground Truth

See `ground-truth.md` for the full ground truth document with annotator agreement.

## Date

2026-03-15
