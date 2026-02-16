# Difference: Workboard Tools Fix

Two files were modified to fix workboard tool support for Memento-S workers.

---

## File 1: `core/skill_engine/planning.py`

### Change: Add workboard ops to LLM system prompt (line 44)

```diff
 "search_files/file_exists), web ops (web_search/search/google_search/fetch/fetch_url/fetch_markdown), "
-"and uv ops (check/install/list). "
+"uv ops (check/install/list), and workboard ops (read_workboard/edit_workboard). "
 "For call_skill include 'skill' and 'plan' (or 'ops'). "
```

**Why:** The LLM system prompt listed allowed op types with a closed list (`"bridge-friendly op types only"`). `read_workboard` and `edit_workboard` were not included, so the LLM would never emit them even when workboard instructions were injected into the subtask.

---

## File 2: `core/skill_engine/skill_executor.py`

### Change: Rewrite `execute_skill_plan()` to pre-extract workboard ops (lines 1149-1200)

#### Before

```python
def execute_skill_plan(skill_name: str, plan: dict) -> str:
    """Top-level entry point: execute a skill plan by name."""
    normalized = normalize_plan_shape(plan)
    skill = str(skill_name or "").strip()
    if skill:
        normalized["_skill_context"] = _coerce_skill_context(normalized, skill)
    log_event("execute_skill_plan_input", skill_name=skill_name, normalized_plan=normalized)
    ops = normalized.get("ops")
    if not isinstance(ops, list) or not ops:
        result = "ERR: no ops provided"
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result

    if skill == "skill-creator":
        result = _execute_skill_creator_plan(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result
    if skill == "filesystem":
        result = _execute_filesystem_ops(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result
    if skill == "terminal":
        result = _execute_terminal_ops(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result
    if skill == "web-search":
        result = _execute_web_ops(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result
    if skill == "uv-pip-install":
        result = _execute_uv_pip_ops(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result
    if skill == "workboard":
        result = _execute_workboard_ops(normalized)
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result

    # Generic skill: dispatch each op individually through the bridge
    outputs: list[str] = []
    for idx, raw_op in enumerate(ops, start=1):
        op = _normalize_op_dict(raw_op)
        if not op:
            outputs.append(f"[op#{idx}] SKIP: op is not a dict")
            continue
        op_type = str(op.get("type") or "unknown")
        out = _dispatch_bridge_op(op, normalized, skill)
        outputs.append(f"[op#{idx}:{op_type}]\n{out}")

    result = "\n\n".join(outputs) if outputs else "ERR: no executable ops"
    log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
    return result
```

#### After

```python
def execute_skill_plan(skill_name: str, plan: dict) -> str:
    """Top-level entry point: execute a skill plan by name."""
    normalized = normalize_plan_shape(plan)
    skill = str(skill_name or "").strip()
    if skill:
        normalized["_skill_context"] = _coerce_skill_context(normalized, skill)
    log_event("execute_skill_plan_input", skill_name=skill_name, normalized_plan=normalized)
    ops = normalized.get("ops")
    if not isinstance(ops, list) or not ops:
        result = "ERR: no ops provided"
        log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
        return result

    # ── Pre-extract workboard ops (new tools, orthogonal to any skill) ──
    workboard_results: list[str] = []
    if skill != "workboard":
        remaining_ops = []
        wb_ops = []
        for op in ops:
            op_type_raw = (
                str(op.get("type") or "").strip().lower()
                if isinstance(op, dict) else ""
            )
            if op_type_raw in WORKBOARD_OP_TYPES:
                wb_ops.append(op)
            else:
                remaining_ops.append(op)
        if wb_ops:
            workboard_results.append(_execute_workboard_ops({"ops": wb_ops}))
        ops = remaining_ops
        normalized["ops"] = ops
        if not ops:
            result = "\n".join(workboard_results)
            log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
            return result

    # ── Dispatch to skill executor ──
    if skill == "skill-creator":
        skill_result = _execute_skill_creator_plan(normalized)
    elif skill == "filesystem":
        skill_result = _execute_filesystem_ops(normalized)
    elif skill == "terminal":
        skill_result = _execute_terminal_ops(normalized)
    elif skill == "web-search":
        skill_result = _execute_web_ops(normalized)
    elif skill == "uv-pip-install":
        skill_result = _execute_uv_pip_ops(normalized)
    elif skill == "workboard":
        skill_result = _execute_workboard_ops(normalized)
    else:
        # Generic skill: dispatch each op individually through the bridge
        outputs: list[str] = []
        for idx, raw_op in enumerate(ops, start=1):
            op = _normalize_op_dict(raw_op)
            if not op:
                outputs.append(f"[op#{idx}] SKIP: op is not a dict")
                continue
            op_type = str(op.get("type") or "unknown")
            out = _dispatch_bridge_op(op, normalized, skill)
            outputs.append(f"[op#{idx}:{op_type}]\n{out}")
        skill_result = "\n\n".join(outputs) if outputs else "ERR: no executable ops"

    # ── Merge workboard + skill results ──
    if workboard_results:
        result = "\n\n".join(workboard_results) + "\n\n" + skill_result
    else:
        result = skill_result
    log_event("execute_skill_plan_output", skill_name=skill_name, result=result)
    return result
```

**Why:** When a worker was routed to a builtin skill (e.g. `"filesystem"`), ALL ops were sent directly to that skill's executor (e.g. `_execute_filesystem_ops()`). Workboard ops like `read_workboard` were not recognized by these executors and returned `"unknown op_type: read_workboard"`.

**What the new code does:**

1. Before dispatching to any builtin executor, scans the ops list and pulls out any workboard ops
2. Executes extracted workboard ops via `_execute_workboard_ops()`
3. Passes remaining ops to the appropriate builtin executor (unchanged logic)
4. Merges workboard results with skill results before returning
5. Skips pre-extraction when `skill == "workboard"` (already handled natively)

---

## Summary

| File | Lines Changed | What |
|------|--------------|------|
| `planning.py` | 1 line (line 44) | Added `workboard ops (read_workboard/edit_workboard)` to LLM allowed ops list |
| `skill_executor.py` | ~52 lines replaced (lines 1149-1200) | Added workboard op pre-extraction + merge in `execute_skill_plan()` |

No other files or functions were modified.
