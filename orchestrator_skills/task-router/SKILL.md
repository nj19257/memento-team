---
name: task-router
description: Identifies the task type and directs the orchestrator to the correct decompose skill.
---

## How to Use
1. Read the user's query
2. Match it against the task types below
3. Call `read_orchestrator_skill("decompose-<matched_type>")` to load the specialized strategy

## Task Types

### annual-rank-stats
**Match when:** The query asks for periodic statistical reports, official rankings, or award lists organized by year or rank (e.g., "Top 100 companies 2010-2020" or "Annual GDP growth by country").
**Load skill:** `decompose-annual-rank-stats`
**Key signal:** Query uses "Year" or "Rank" as the primary unique identifier for the data rows.

### temporal-event-logs
**Match when:** The request involves tracking specific occurrences, performances, or appearances over a timeline. Usually involves high row counts and precise dates.
**Load skill:** `decompose-temporal-event-logs`
**Key signal:** Query mentions "tours," "match results," "concerts," or "chronological history" of a specific subject.

### geographic-registries
**Match when:** The task requires listing protected sites, parks, or corporate entities within specific administrative or geographic boundaries.
**Load skill:** `decompose-geographic-registries`
**Key signal:** Query mentions "National Parks," "UNESCO sites," "registered companies in [Region]," or "official registry."

### entity-benchmarking
**Match when:** The query requires comparing multiple organizations, products, or institutions based on technical specs, metrics, or attributes.
**Load skill:** `decompose-entity-benchmarking`
**Key signal:** Query mentions multiple specific brands, universities, or vehicle models for side-by-side comparison.

## Default Fallback
If no type matches clearly, use general decomposition principles:
- Split by entity or category
- Keep each worker under 30 rows
- List all required columns in every subtask