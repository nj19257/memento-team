---
name: task-router
description: Identifies the task type and directs the orchestrator to the correct decompose skill.
---

## How to Use
1. Read the user's query and identify the primary organizational axis (time, entity, category, or rank).
2. Match it against the task types below.
3. Call `read_orchestrator_skill("decompose-<matched_type>")` to load the specialized strategy.

## Task Types

### split-by-rank-segment
**Match when:** The query asks for a specific "Top N" list or a numbered ranking (e.g., "Top 50 movies," "100 best-selling albums"). The request relies on a pre-existing ordinal sequence.
**Load skill:** `decompose-split-by-rank-segment`
**Key signal:** Presence of ordinal numbers, "Top [X]," or "Ranked" phrasing.

### split-by-time-period
**Match when:** The query specifies a continuous chronological range or a multi-year history (e.g., "from 2010 to 2024," "all releases in the 1990s").
**Load skill:** `decompose-split-by-time-period`
**Key signal:** Date ranges, decades, or "year-by-year" requirements.

### split-by-entity
**Match when:** The query lists specific, discrete subjects like brand names, individual people, or specific product models that require deep attribute extraction (e.g., "Nikon Z6, Sony A7IV, and Canon R6").
**Load skill:** `decompose-split-by-entity`
**Key signal:** Proper nouns of specific products, companies, or individuals.

### split-by-category
**Match when:** The query is organized by broad domain classifications, geographic regions, or institutional departments (e.g., "by country," "academic subjects," or "sports leagues").
**Load skill:** `decompose-split-by-category`
**Key signal:** Use of "by [Category Name]" or lists of distinct sectors/regions.

## Default Fallback
If no type matches clearly, use general decomposition principles:
- Split by entity or category to ensure data independence.
- Keep each worker load under 30 rows to prevent context loss.
- Explicitly list all required columns/attributes in every subtask description.