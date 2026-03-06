---
name: verify
description: Post-synthesis verification for broad information-seeking tasks. Use this skill after workers return results and before producing the final output, to check for missing rows, incomplete columns, and data accuracy issues. Especially important when the task asks for a comprehensive table or dataset.
---

## When to Use
After receiving worker results for any task that produces a structured table or dataset —
especially when the user asks for "all X", "comprehensive list", or a multi-column table.
Read this skill right after `execute_subtasks` returns, before writing the final response.

## Verification Process

### Step 1: Row Completeness Check
Count the rows you have and compare against the expected total.

- If the task specifies a number (e.g., "top 5 per category × 5 categories" = 25 rows), verify you have that many.
- If the task asks for "all items in category X", check: did the enumeration subtask return a complete list? Are there known subcategories that might be missing?
- **Action if rows are missing**: dispatch follow-up subtasks targeting the specific missing items by name.

### Step 2: Column Completeness Check
For each row, verify every required column has a non-empty value.

- Read through the worker results and flag any cells that are empty, contain "N/A", or say "not found".
- Pay special attention to columns that require deep-web data (application fees, specific dates, pricing) — these are the most likely to be missing or truncated.
- **Action if columns are incomplete**: dispatch targeted follow-up subtasks: "Find the [specific column] for [specific entity]. Search the official website at [URL if known]."

### Step 3: Cross-Validation
When multiple workers returned data for the same entity, compare their values.

- If Worker A says Harvard's fee is $105 but Worker B says $85, flag the conflict.
- Prefer values sourced from official websites over aggregator sites.
- For numerical data (fees, rankings), exact numbers should match. For dates, the same deadline should appear.
- **Action if conflicts found**: dispatch a verification subtask to check the official source directly.

### Step 4: Format Consistency Check
Before producing the final markdown table:

- Ensure all rows use the same date format (don't mix "Jan 1" and "January 1, 2025")
- Ensure currency symbols are consistent per column
- Ensure ranking numbers are plain digits (no "=" or "~" prefixes)
- Ensure university/entity names use full official names consistently (no mixing "MIT" with "Massachusetts Institute of Technology")
- URLs should be clean (no tracking parameters, consistent with/without trailing slash)

### Step 5: Gap-Fill Decision
After steps 1-4, decide whether to:

- **Proceed**: if coverage is ≥90% and no critical columns are empty → produce the final output
- **Re-dispatch**: if coverage is <90% or a required column is systematically empty → call `execute_subtasks` again with targeted follow-up subtasks for the gaps
- **Report gaps**: if re-dispatch would not help (e.g., data genuinely doesn't exist), note the gaps explicitly in the final output rather than leaving cells empty or guessing

## Common Failure Patterns

| Pattern | Symptom | Fix |
|---------|---------|-----|
| Worker truncation | Last few rows have empty columns | Re-dispatch those rows to a new worker with fewer items |
| Stale data | Fees/dates don't match current year | Explicitly add "as of [year]" to the subtask and require the worker to visit the official site |
| Name mismatch | Same entity appears with different names across workers | Standardize to the full official name before merging |
| Missed subcategory | An entire category/group is absent | Re-check the enumeration step; dispatch a subtask specifically for the missing category |
| Partial URL | URL points to homepage instead of specific program page | Acceptable if the task asked for "homepage"; flag if it asked for a specific page |

## Checklist (run mentally before producing final output)
- [ ] Row count matches expected total?
- [ ] Every required column has a value in every row?
- [ ] No conflicting values between workers for the same entity?
- [ ] Dates, currencies, and names are formatted consistently?
- [ ] If gaps remain, are they explicitly noted (not silently omitted)?
