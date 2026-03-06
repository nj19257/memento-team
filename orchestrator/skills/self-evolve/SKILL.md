---
name: self-evolve
description: ALWAYS runs after receiving worker results. Reviews trajectories to spot failures or inefficiencies; improves orchestrator skills only when clear issues are found. No changes needed is the normal, expected outcome.
---

# Self-Evolve Protocol

## When to Run

This protocol runs EVERY time after synthesizing worker results. Start by reviewing the trajectory headers of all workers. Most of the time, results will be fine and you should conclude with **no changes needed** — that is the normal, expected outcome.

Only make skill improvements if you observe clear, actionable issues:
- A worker **failed** (appears in the `failed` list or result starts with "FAILED:")
- A worker took **unusually long** relative to task complexity
- A worker result looks **incorrect, incomplete, or suspicious** relative to the subtask
- Multiple workers produced **redundant or contradictory** results
- A worker's result indicates it was **confused by the subtask description** (e.g., asked clarifying questions, did something unrelated)

If results are decent, simply confirm "No improvements needed" and stop. Do NOT force changes when things are working well.

## Step 1: Read Worker Trajectories

Each result dict from `execute_subtasks` includes a `trajectory_file` path. Start by reading just the header (line 1) for quick triage:

```
view(description="Triage worker trajectory header", path="<trajectory_file>", view_range=[1, 1])
```

The header JSON contains: `status`, `time_taken_seconds`, `total_events`, `result_preview`.

If the header indicates an issue, read more of the trajectory for details:

```
view(description="Read full worker trajectory for analysis", path="<trajectory_file>")
```

Key events to examine:
- **`tool_call_start` / `tool_call_end`:** What tools were called, how long each took, any errors in `result_preview`
- **`tool_call_error`:** Explicit tool failures
- **`worker_attempt_start`:** If `attempt > 1`, the worker retried (indicates instability)
- **`worker_end`:** Final status and duration

## Step 2: Diagnose the Root Cause

Classify the issue into one of these categories:

### Routing Failures → fix `task-router`
- **Wrong type matched:** The router sent the task to the wrong `decompose-<type>` skill, leading to an inappropriate decomposition strategy
- **No match when one exists:** The task clearly fits an existing type but fell through to the default fallback
- **Fix:** Add/refine match criteria or key signals in the relevant task type section of `task-router`

### Strategy Failures → fix the specific `decompose-<type>` skill
- **Missing context:** Worker was confused because the subtask description lacked necessary information (file paths, parameter values, background context)
- **Task too broad:** Worker took too long or produced unfocused results because the subtask scope was too wide
- **Task too narrow:** Multiple subtasks could have been one, causing unnecessary overhead
- **Inter-dependency:** A subtask implicitly depended on another worker's result (violating statelessness)
- **Bad slicing:** Task elements were split in a way that caused redundant work across workers
- **Fix:** Read and improve the specific `decompose-<type>` skill that was used for this task

### New Task Type → create a new `decompose-<type>` skill + update `task-router`
- **Repeated fallback:** The task did not match any existing type, AND the fallback decomposition produced poor results
- **Fix:** Create a new skill file at `orchestrator/skills/decompose-<new-type>/SKILL.md` using `file_create`, following the format of existing decompose skills. Then add a new section to `task-router` with match criteria and key signals.

### Coordination Failures → fix `workboard-protocol`
- **Tag confusion:** Worker edited the wrong workboard tags or could not find its tags
- **Missing slots:** Workboard did not have enough tagged sections for the subtasks
- **No shared context:** Workers needed shared information that was not in the workboard
- **Result format mismatch:** Workers wrote results in inconsistent formats making synthesis hard

### Worker-Side Issues → do NOT fix skills for these
- **Transient API errors:** Network timeouts, rate limits — not a skill problem
- **Tool bugs:** Worker's own tool failures unrelated to decomposition — not an orchestrator skill problem
- **Model hallucinations:** Worker fabricated data — not fixable via decomposition changes

## Step 3: Improve the Relevant Skill

First, read the current skill content:
```
read_skill("<skill-to-fix>")
```

Then apply **targeted, incremental** improvements using `str_replace`:

```
str_replace(
  description="Improving <skill-name> based on trajectory analysis",
  path="orchestrator/skills/<skill-name>/SKILL.md",
  old_str="<exact text to replace>",
  new_str="<improved text>"
)
```

To create a new decompose skill:
```
file_create(
  description="Create new decompose skill for <type>",
  path="orchestrator/skills/decompose-<type>/SKILL.md",
  file_text="---\nname: decompose-<type>\ndescription: <one-line description>\n---\n\n<skill content>"
)
```
Then update `task-router` to add a new section referencing it.

### Improvement Guidelines

1. **Be specific:** Add concrete examples from the failure. Instead of "provide more context", write "When subtasks involve file operations, always include the full absolute file path and expected file format."

2. **Be incremental:** Change one section at a time. Never rewrite the entire skill.

3. **Preserve structure:** Keep the YAML frontmatter, section headers, and overall format intact.

4. **Add, don't remove:** Prefer adding new bullet points or examples over deleting existing guidance that may be correct for other cases.

5. **Test mentally:** Before applying a change, verify it would have prevented the observed failure without breaking other scenarios.

6. **Document the change:** Add a brief HTML comment at the bottom of the skill noting what was changed and why:
   ```
   <!-- Self-evolve update [YYYY-MM-DD]: Added guidance on X based on worker trajectory analysis showing Y -->
   ```

## Safety Rules

- **NEVER** delete or empty a skill file. Always use `str_replace` with specific old/new strings.
- **NEVER** change the YAML frontmatter (`name` and `description` fields).
- **NEVER** make changes based on a single transient error (network timeout, API rate limit).
- **ALWAYS** read the current skill content before editing to ensure `old_str` matches exactly.
- **ALWAYS** use a sufficiently large `old_str` block to ensure uniqueness in the file.
- If unsure whether a change is warranted, **skip it**.

## Example Analysis

Worker trajectory shows:
- Task was routed to `decompose-annual-rank-stats` but the query was actually about comparing product specs
- Workers returned yearly rankings instead of side-by-side specs

**Diagnosis:** Routing failure. The router matched "rank" in the query but the task was entity-benchmarking.

**Fix for `task-router`:**
```
str_replace(
  description="Clarify annual-rank-stats vs entity-benchmarking distinction",
  path="orchestrator/skills/task-router/SKILL.md",
  old_str="**Key signal:** Query uses \"Year\" or \"Rank\" as the primary unique identifier for the data rows.",
  new_str="**Key signal:** Query uses \"Year\" or \"Rank\" as the primary unique identifier for the data rows. Note: if the query asks for side-by-side comparison of specific entities (even if rankings are mentioned), prefer `entity-benchmarking` instead."
)
```
