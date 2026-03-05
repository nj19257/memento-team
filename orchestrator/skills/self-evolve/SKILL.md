---
name: self-evolve
description: Self-reflection and skill improvement protocol. Analyzes worker trajectories to identify decomposition and coordination failures, then improves orchestrator skills.
---

# Self-Evolve Protocol

## When to Trigger

After `execute_subtasks` returns, evaluate results for any of these signals:
- A worker **failed** (appears in the `failed` list or result starts with "FAILED:")
- A worker took **unusually long** 
- A worker result looks **incorrect, incomplete, or suspicious** relative to the subtask
- Multiple workers produced **redundant or contradictory** results
- A worker's result indicates it was **confused by the subtask description** (e.g., asked clarifying questions, did something unrelated)

If none of these signals are present, skip self-evolve entirely.

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

### Decomposition Failures → fix `decompose-strategy`
- **Missing context:** Worker was confused because the subtask description lacked necessary information (file paths, parameter values, background context)
- **Task too broad:** Worker took too long or produced unfocused results because the subtask scope was too wide
- **Task too narrow:** Multiple subtasks could have been one, causing unnecessary overhead
- **Inter-dependency:** A subtask implicitly depended on another worker's result (violating statelessness)
- **Bad slicing:** Task elements were split in a way that caused redundant work across workers

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
read_skill("decompose-strategy")
read_skill("workboard-protocol")
```

Then apply **targeted, incremental** improvements using `str_replace`:

```
str_replace(
  description="Improving decompose-strategy based on trajectory analysis",
  path="orchestrator/skills/decompose-strategy/SKILL.md",
  old_str="<exact text to replace>",
  new_str="<improved text>"
)
```

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
- **Limit to 1-2 improvements per self-evolve cycle.** Do not over-correct.
- If unsure whether a change is warranted, **skip it**.

## Example Analysis

Worker trajectory shows:
- Worker was asked to "find details about config"
- Worker called `bash_tool` with `find / -name config` (too broad, searched entire filesystem)
- Worker timed out after 120 seconds

**Diagnosis:** Missing context in decomposition. The subtask said "config" without specifying which config file or directory.

**Fix for `decompose-strategy`:**
```
str_replace(
  description="Add guidance about specifying file paths in subtask descriptions",
  path="orchestrator/skills/decompose-strategy/SKILL.md",
  old_str="- GOOD: \"Read the file /home/user/project/config.py and extract the database URL\"\n- BAD: \"Read the config file mentioned earlier\"",
  new_str="- GOOD: \"Read the file /home/user/project/config.py and extract the database URL\"\n- BAD: \"Read the config file mentioned earlier\"\n- GOOD: \"Search for Python files in /home/user/project/src/ that import the requests library\"\n- BAD: \"Find files that use requests\""
)
```
