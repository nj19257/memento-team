"""Skill routing logic: explicit matching, semantic selection, LLM-based routing."""

import json
import re
from pathlib import Path
from typing import Any

from core.config import (
    AGENTS_MD,
    DEBUG,
    SEMANTIC_ROUTER_ENABLED,
    SEMANTIC_ROUTER_TOP_K,
    SEMANTIC_ROUTER_DEBUG,
    SEMANTIC_ROUTER_WRITE_VISIBLE_AGENTS,
    SEMANTIC_ROUTER_CATALOG_MD,
    SEMANTIC_ROUTER_CATALOG_JSONL,
    SEMANTIC_ROUTER_BASE_SKILLS,
)
from core.utils.logging_utils import log_event
from core.llm import openrouter_messages
from core.utils.json_utils import parse_json_output
from core.utils.path_utils import _truncate_middle
from core.skill_engine.skill_catalog import (
    load_available_skills_block_from,
    parse_available_skills,
    build_available_skills_xml,
    write_visible_skills_block,
    select_semantic_top_skills,
    _load_router_catalog_from_jsonl,
    _merge_skill_catalog,
    build_router_step_note,
    derive_semantic_goal,
)


def explicit_skill_match(user_text: str, skill_names: list[str]) -> str | None:
    """Match user text against known skill names using word-boundary regex."""
    for name in skill_names:
        if re.search(rf"\b{re.escape(name)}\b", user_text, re.IGNORECASE):
            return name
    return None


def _normalize_router_decision(obj: Any) -> dict:
    """Normalize router JSON output into a standard decision dict.

    Accepts various LLM response shapes (dict with action, list of steps,
    old workflow format, etc.) and returns a dict with at least an ``action``
    key set to one of ``"next_step"``, ``"done"``, or ``"none"``.
    """
    if isinstance(obj, dict):
        action = obj.get("action")
        # Normalize action names
        if action in ("next_step", "step", "execute"):
            obj = {**obj, "action": "next_step"}
        elif action in ("done", "complete", "finished"):
            obj = {**obj, "action": "done"}
        elif action in ("load_skill", "skill"):
            obj = {**obj, "action": "next_step"}  # Treat as next_step
        elif isinstance(action, str) and action.strip():
            # Some models emit the skill name directly as "action"
            # (e.g. {"action":"web-search","name":"web-search",...}).
            # Normalize this shape to next_step so executors will run it.
            skill_name = str(obj.get("name") or "").strip()
            action_name = action.strip()
            if skill_name:
                obj = {**obj, "action": "next_step"}
            elif re.fullmatch(r"[a-z0-9][a-z0-9-]*", action_name):
                obj = {
                    **obj,
                    "action": "next_step",
                    "name": action_name,
                    "reason": obj.get("reason") or "router_action_as_skill_name",
                }
        elif not action:
            # Infer action from structure
            if isinstance(obj.get("steps"), list):
                # Old workflow format - convert first step to next_step
                steps = obj.get("steps")
                if steps:
                    first = steps[0]
                    return {
                        "action": "next_step",
                        "name": first.get("name") or first.get("skill"),
                        "user": first.get("user") or first.get("user_text"),
                        "reason": obj.get("reason") or "router_normalized",
                    }
                return {"action": "none", "reason": "empty_steps"}
            elif isinstance(obj.get("name"), str) and obj.get("name").strip():
                obj = {
                    **obj,
                    "action": "next_step",
                    "reason": obj.get("reason") or "router_normalized",
                }
            else:
                obj = {
                    **obj,
                    "action": "none",
                    "reason": obj.get("reason") or "router_normalized",
                }
        return obj

    if isinstance(obj, list):
        if not obj:
            return {"action": "none", "reason": "router_empty_list"}
        for item in obj:
            if isinstance(item, dict) and item.get("action"):
                return _normalize_router_decision(item)
        # Old workflow list format - convert first step
        if all(isinstance(item, dict) for item in obj):
            first = obj[0]
            return {
                "action": "next_step",
                "name": first.get("name") or first.get("skill"),
                "user": first.get("user") or first.get("user_text"),
                "reason": "router_list_first_step",
            }
        return {"action": "none", "reason": "router_invalid_list"}

    return {"action": "none", "reason": f"router_invalid_type:{type(obj).__name__}"}


def route_skill(
    user_text: str,
    skills: list[dict],
    skills_xml: str,
    *,
    allow_new_skills: bool = True,
    context: list[str] | None = None,
    routing_goal: str | None = None,
) -> dict:
    """Route user request to skill(s). Can be called iteratively for dynamic workflows.

    Args:
        user_text: Original user request
        skills: List of available skills
        skills_xml: XML representation of skills
        allow_new_skills: Whether to allow creating new skills
        context: List of previous step outputs (for dynamic workflow continuation)
        routing_goal: Goal text used for semantic candidate retrieval before LLM routing
    """
    goal_text = str(routing_goal or user_text or "").strip() or user_text
    log_event(
        "route_skill_input",
        user_text=user_text,
        routing_goal=routing_goal,
        computed_goal=goal_text,
        context=context,
        skills_count=len(skills),
        allow_new_skills=allow_new_skills,
    )

    visible_skills = skills
    visible_skills_xml = skills_xml
    semantic_catalog_skills = skills
    catalog_loaded_ok = False
    catalog_source = "runtime"

    # ------------------------------------------------------------------
    # 1. Load extended catalog from JSONL (preferred) or MD fallback
    # ------------------------------------------------------------------
    if SEMANTIC_ROUTER_CATALOG_JSONL:
        try:
            catalog_skills, _by_name = _load_router_catalog_from_jsonl(SEMANTIC_ROUTER_CATALOG_JSONL)
            if catalog_skills:
                semantic_catalog_skills = _merge_skill_catalog(catalog_skills, skills)
                catalog_loaded_ok = True
                catalog_source = "jsonl"
            elif SEMANTIC_ROUTER_DEBUG:
                print(
                    f"[semantic-router] empty JSONL catalog at {SEMANTIC_ROUTER_CATALOG_JSONL!r}; "
                    "fallback to AGENTS catalog"
                )
        except Exception as exc:
            if SEMANTIC_ROUTER_DEBUG:
                print(f"[semantic-router] failed to load JSONL catalog {SEMANTIC_ROUTER_CATALOG_JSONL!r}: {exc}")

    if not catalog_loaded_ok and SEMANTIC_ROUTER_CATALOG_MD:
        try:
            catalog_xml = load_available_skills_block_from(SEMANTIC_ROUTER_CATALOG_MD)
            catalog_skills = parse_available_skills(catalog_xml)
            if catalog_skills:
                semantic_catalog_skills = _merge_skill_catalog(catalog_skills, skills)
                catalog_loaded_ok = True
                catalog_source = "xml"
            elif SEMANTIC_ROUTER_DEBUG:
                print(f"[semantic-router] empty catalog at {SEMANTIC_ROUTER_CATALOG_MD!r}; fallback to AGENTS_MD")
        except Exception as exc:
            if SEMANTIC_ROUTER_DEBUG:
                print(f"[semantic-router] failed to load catalog {SEMANTIC_ROUTER_CATALOG_MD!r}: {exc}")

    # ------------------------------------------------------------------
    # 2. Semantic top-K selection (TF-IDF)
    # ------------------------------------------------------------------
    if SEMANTIC_ROUTER_ENABLED and semantic_catalog_skills:
        selected = select_semantic_top_skills(
            goal_text, semantic_catalog_skills, top_k=SEMANTIC_ROUTER_TOP_K
        )
        if selected:
            visible_skills = selected
            visible_skills_xml = build_available_skills_xml(selected)
            if SEMANTIC_ROUTER_WRITE_VISIBLE_AGENTS:
                if catalog_source == "xml" and SEMANTIC_ROUTER_CATALOG_MD and catalog_loaded_ok:
                    same_file = False
                    try:
                        same_file = Path(SEMANTIC_ROUTER_CATALOG_MD).resolve() == Path(AGENTS_MD).resolve()
                    except Exception:
                        same_file = SEMANTIC_ROUTER_CATALOG_MD == AGENTS_MD
                    if same_file:
                        if SEMANTIC_ROUTER_DEBUG:
                            print(
                                "[semantic-router] skip writing visible AGENTS because "
                                "SEMANTIC_ROUTER_CATALOG_MD == AGENTS_MD"
                            )
                    else:
                        write_visible_skills_block(visible_skills_xml, AGENTS_MD)
                elif SEMANTIC_ROUTER_DEBUG:
                    print(
                        "[semantic-router] skip writing visible AGENTS because "
                        "catalog source is not AGENTS-style XML"
                    )
            if SEMANTIC_ROUTER_DEBUG:
                names = ", ".join(str(s.get("name") or "").strip() for s in selected)
                print(f"[semantic-router] goal={goal_text!r} visible={len(selected)} skills: {names}")
            log_event(
                "semantic_router_selected",
                goal_text=goal_text,
                selected_skills=[str(s.get("name") or "").strip() for s in selected],
                selected_count=len(selected),
                catalog_count=len(semantic_catalog_skills),
                catalog_source=catalog_source,
            )

    # ------------------------------------------------------------------
    # 3. Explicit skill-name match (first call only, no context)
    # ------------------------------------------------------------------
    if not context:
        skill_names = [
            str(s.get("name") or "").strip()
            for s in semantic_catalog_skills
            if isinstance(s, dict) and str(s.get("name") or "").strip()
        ]
        explicit = explicit_skill_match(user_text, skill_names)
        if explicit:
            decision = {
                "action": "next_step",
                "name": explicit,
                "user": user_text,
                "reason": "explicit_match",
            }
            log_event("route_skill_output", decision=decision, mode="explicit_match")
            return decision

    # ------------------------------------------------------------------
    # 4. Build prompt for LLM-based routing
    # ------------------------------------------------------------------
    if allow_new_skills:
        rules = (
            "- Prefer using existing skills in <available_skills> below.\n"
            "- If none fit, you MAY invent a new skill name (lowercase kebab-case) and plan to use it.\n"
            "- New skill names should be short (2-4 words) and capability-focused (not task-id-specific).\n"
        )
        available_label = "Available skills (existing):"
    else:
        rules = "- Use ONLY skills listed in <available_skills> below.\n"
        available_label = "Available skills (authoritative):"

    # Build context section if we have previous steps
    context_section = ""
    if context:
        context_section = (
            "\n\n=== COMPLETED STEPS AND THEIR OUTPUTS ===\n"
            + "\n\n".join(context)
            + "\n=== END OF COMPLETED STEPS ===\n\n"
            "IMPORTANT: Analyze the outputs above carefully.\n"
            '- If the original task is FULLY COMPLETED based on these outputs, return {"action":"done"}\n'
            "- If more work is needed, return the NEXT step (do NOT repeat a completed step)\n"
            "- Each step should make NEW progress, not repeat previous work"
        )

    prompt = f"""
You are a skill router and workflow planner.

Return ONLY JSON in ONE of these forms:

1) Execute next skill step (only if more work needed):
{{"action":"next_step","name":"<skill-name>","user":"instruction for this step only","reason":"short"}}

2) No skill needed (answer directly in normal chat):
{{"action":"done","reason":"no_skill_needed"}}

3) Task complete (when the user's request has been fulfilled):
{{"action":"done","reason":"explain what was accomplished"}}

4) No skill matches but a skill/tool would be required to complete the task:
{{"action":"none","reason":"short"}}

CRITICAL Rules:
{rules}- CAREFULLY analyze completed steps before deciding.
- IMPORTANT: If the user request involves ACTUAL ACTIONS like saving/writing files, running commands, searching the web, installing packages, etc., you MUST use the appropriate skill (e.g., filesystem for file operations, terminal for shell commands). Do NOT return "done" for action requests - that would skip the actual execution!
- If you are uncertain about correctness/completeness/freshness, first check available skills and route to a helpful one instead of answering directly (especially use web-search for factual lookups).
- For non-trivial questions or instructions, prefer a skill step when any available skill can materially improve answer quality.
- Only return {{"action":"done","reason":"no_skill_needed"}} for very simple, high-confidence conversational requests (e.g., greetings, small talk, basic explanations) that clearly do not need tool support.
- Return "done" if the user's original request has been satisfied (after skills have executed).
- Return "none" ONLY when the user clearly needs an external action/tool AND none of the available skills apply.
- Do NOT repeat steps that have already been completed successfully.
- Each new step must make DIFFERENT progress than previous steps.

{available_label}
{visible_skills_xml}

User request:
{user_text}
{context_section}
""".strip()
    log_event(
        "route_prompt",
        goal_text=goal_text,
        available_label=available_label,
        visible_skills=[str(s.get("name") or "").strip() for s in visible_skills],
        context_count=(len(context) if isinstance(context, list) else 0),
    )

    # ------------------------------------------------------------------
    # 5. LLM call for routing decision
    # ------------------------------------------------------------------
    output = openrouter_messages(
        "Return only valid JSON.",
        [{"role": "user", "content": prompt}],
    )
    log_event("route_raw_output", raw_output=output)

    # ------------------------------------------------------------------
    # 6. Parse and normalize the response
    # ------------------------------------------------------------------
    try:
        decision = _normalize_router_decision(json.loads(output))
        log_event("route_skill_output", decision=decision, mode="json")
        return decision
    except json.JSONDecodeError:
        parsed = parse_json_output(output)
        if parsed:
            decision = _normalize_router_decision(parsed)
            log_event("route_skill_output", decision=decision, mode="parsed_json_fragment")
            return decision
        preview = (output or "").strip().replace("\n", "\\n")
        if DEBUG:
            print(f"[debug] route_skill invalid JSON output preview={preview[:500]!r}")
        decision = {"action": "none", "reason": "router_invalid_json"}
        log_event("route_skill_output", decision=decision, mode="invalid_json")
        return decision
