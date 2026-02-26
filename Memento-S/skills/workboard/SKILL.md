---
name: workboard
description: Use this skill when a task includes a shared workboard for multi-worker coordination. It explains how to read the board, identify your assigned subtask ID (for example t1 or t2), and fill only your own tagged sections using read_workboard and edit_workboard(tag, content).
---

# Workboard Coordination

Use this skill when the task prompt includes a shared workboard and a subtask ID such as `t1`, `t2`, etc.

## Goal

Coordinate safely with other workers by writing only to your assigned tagged sections in `.workboard.md`.

## Tools

- `read_workboard()` reads the full board
- `read_workboard(tag)` reads one tagged section (for example `t1_result`)
- `edit_workboard(tag, content)` replaces the content inside `<tag>...</tag>`

## Required behavior

1. Identify your subtask ID from the prompt (for example `t1`).
2. Read the workboard before writing:
   - Read the full board or your relevant tags.
3. Only edit tags that belong to your subtask ID:
   - Allowed examples for `t1`: `t1_status`, `t1_result`, `t1_artifacts`
   - Do not edit `t2_*`, `t3_*`, or manager tags.
4. Write concise, structured updates.
5. Prefer a status/result split:
   - `tN_status`: `done`, `partial`, or `blocked`
   - `tN_result`: short markdown summary
   - `tN_artifacts`: file paths / URLs / outputs (if present)

## Suggested workflow

1. `read_workboard()` to inspect the board structure and confirm your tags exist.
2. Do the assigned task.
3. `edit_workboard("<your_id>_status", "done")` (or `partial` / `blocked`)
4. `edit_workboard("<your_id>_result", "<concise result summary>")`
5. If needed, `edit_workboard("<your_id>_artifacts", "<paths or links>")`

## Example

If your subtask ID is `t2` and the board contains:

```xml
<t2_status></t2_status>
<t2_result></t2_result>
```

Then write:

- `edit_workboard("t2_status", "done")`
- `edit_workboard("t2_result", "- Found 3 relevant sources\n- Key finding: ...")`

## Notes

- `edit_workboard` replaces the entire tag content. Read first if you need to preserve existing text.
- If a tag is missing, report that in your final response instead of editing another tag.
