"""Self-reflection: analyze verify_report.json → generate decompose-strategy SKILL.md.

Usage:
    python eval/reflect.py                         # Default: read verify_report.json
    python eval/reflect.py --report path/to.json   # Custom report path

Outputs:
    orchestrator_skills/decompose-strategy/SKILL.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.utils import (
    REPORTS_DIR,
    call_gemini_flash,
    load_tasks,
    default_task_ids,
)

SKILL_OUTPUT_DIR = PROJECT_ROOT / "orchestrator_skills" / "decompose-strategy"


def build_reflection_prompt(report: dict) -> str:
    """Build the prompt for Gemini Flash to generate decomposition strategy rules."""

    summary = report.get("summary", {})
    missing_cats = report.get("missing_categories", [])
    incomplete_cols = report.get("incomplete_columns", {})
    decomp_issues = report.get("decomposition_issues", [])
    compressed_trajs = report.get("compressed_trajectories", {})

    # Load task definitions for context
    task_ids = default_task_ids()
    tasks = load_tasks(task_ids)
    task_schemas = []
    for t in tasks:
        eval_spec = t["evaluation"]
        task_schemas.append({
            "instance_id": t["instance_id"],
            "query_preview": t["query"][:200],
            "unique_columns": eval_spec["unique_columns"],
            "required_columns": eval_spec.get("required", []),
            "eval_columns": list(eval_spec.get("eval_pipeline", {}).keys()),
        })

    prompt = f"""You are an AI systems engineer analyzing evaluation results from a multi-agent orchestrator system.

## Context
The system uses an Orchestrator Agent that decomposes tasks into subtasks dispatched to parallel workers.
Each worker is stateless and performs web searches to fill in parts of a large information table.
The system was evaluated on WideSearch benchmark tasks (broad information-seeking that produces markdown tables).

## Evaluation Results Summary
- Tasks evaluated: {summary.get('total_tasks', 0)}
- Row recall: {summary.get('row_recall', 0):.2%} (matched/total gold rows)
- Average cell accuracy: {summary.get('average_cell_accuracy', 0):.2%}
- Missing rows: {summary.get('total_missing_rows', 0)}/{summary.get('total_gold_rows', 0)}

## Error Patterns

### Missing Categories (rows not found)
{json.dumps(missing_cats, indent=2, ensure_ascii=False)[:3000]}

### Incomplete Columns (accuracy < 80%)
{json.dumps(incomplete_cols, indent=2, ensure_ascii=False)[:2000]}

### Decomposition Issues
{json.dumps(decomp_issues, indent=2, ensure_ascii=False)[:2000]}

### Task Schemas (what columns each task expected)
{json.dumps(task_schemas, indent=2, ensure_ascii=False)[:3000]}

### Compressed Trajectories (how the orchestrator actually decomposed tasks)
{json.dumps(compressed_trajs, indent=2, ensure_ascii=False)[:4000]}

## Your Task
Based on the error patterns above, extract **actionable decomposition strategy rules** for the orchestrator.

Generate a SKILL.md document in this exact format:

```
---
name: decompose-strategy
description: Strategy guidance for decomposing broad information-seeking tasks into subtasks.
---

## When to Use
[When should the orchestrator consult this skill]

## Key Principles
[3-5 high-level principles derived from the error analysis]

## Decomposition Rules

### Rule 1: [Title]
[Specific, actionable rule with examples]

### Rule 2: [Title]
...

## Common Pitfalls
[List of common mistakes to avoid, derived from the error patterns]

## Verification Checklist
[Checklist the orchestrator should run through before finalizing decomposition]
```

Requirements:
- Rules must be SPECIFIC and ACTIONABLE (not generic advice)
- Reference actual error patterns from the data
- Focus on: task splitting granularity, coverage of subcategories, data completeness per column
- Include concrete examples where possible
- Keep it concise (under 800 words)"""

    return prompt


def generate_strategy(report: dict) -> str:
    """Use Gemini Flash to generate the SKILL.md content from the error report."""
    prompt = build_reflection_prompt(report)
    return call_gemini_flash(prompt)


def clean_skill_content(raw: str) -> str:
    """Extract the SKILL.md content from Gemini Flash's response.

    The model may wrap it in a code block.
    """
    import re

    # Try to extract from code block
    match = re.search(r"```(?:markdown|md)?\s*\n(---.*?)```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If it starts with --- (frontmatter), use as-is
    if raw.strip().startswith("---"):
        return raw.strip()

    # Fallback: wrap in frontmatter
    return f"""---
name: decompose-strategy
description: Strategy guidance for decomposing broad information-seeking tasks into subtasks.
---

{raw.strip()}"""


def main():
    parser = argparse.ArgumentParser(description="WideSearch self-reflection → strategy generation")
    parser.add_argument(
        "--report",
        default=None,
        help="Path to verify_report.json (default: eval/reports/verify_report.json)",
    )
    args = parser.parse_args()

    report_path = Path(args.report) if args.report else REPORTS_DIR / "verify_report.json"

    if not report_path.exists():
        print(f"Error: Report not found at {report_path}")
        print("Run 'python eval/verify.py' first to generate the verification report.")
        sys.exit(1)

    report = json.loads(report_path.read_text(encoding="utf-8"))

    print(f"\nWideSearch Self-Reflection Pipeline")
    print(f"  Report: {report_path}")
    print(f"  Summary: {report.get('summary', {})}")

    # Generate strategy
    print(f"\n  Generating decomposition strategy via Gemini Flash...")
    raw_skill = generate_strategy(report)
    skill_content = clean_skill_content(raw_skill)

    # Save SKILL.md
    SKILL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    skill_path = SKILL_OUTPUT_DIR / "SKILL.md"
    skill_path.write_text(skill_content, encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"  Strategy saved: {skill_path}")
    print(f"  Content preview:")
    print(f"{'=' * 60}")
    for line in skill_content.split("\n")[:30]:
        print(f"  {line}")
    if skill_content.count("\n") > 30:
        print(f"  ... ({skill_content.count(chr(10)) - 30} more lines)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
