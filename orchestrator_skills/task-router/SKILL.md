---
name: task-router
description: Identifies the task type and directs the orchestrator to the correct decompose skill.
---

## How to Use
1. Read the user's query
2. Match it against the task types below
3. Call `read_orchestrator_skill("decompose-<matched_type>")` to load the specialized strategy

## Task Types

### financial-time-series
**Match when:** The query asks for quantitative economic data, stock prices, market caps, or fiscal performance metrics over specific periods.
**Load skill:** `decompose-financial-time-series`
**Key signal:** Mentions of "exchange," "ticker," "revenue," "fiscal year," or "stock price."

### ranked-authority-lists
**Match when:** The query seeks a specific subset of items based on an official ranking, award, or "Top N" list from a specific year.
**Load skill:** `decompose-ranked-authority-lists`
**Key signal:** Mentions of "Top 50," "Award winners," "Ranked by," or "Official list."

### temporal-event-logs
**Match when:** The query requires a chronological list of occurrences, performances, or launches spanning several years or decades.
**Load skill:** `decompose-temporal-event-logs`
**Key signal:** High-volume date-driven requests like "all concerts since 1990" or "launch history."

### geo-institutional-data
**Match when:** The query focuses on physical locations, administrative jurisdictions, or public/government institutions.
**Load skill:** `decompose-geo-institutional-data`
**Key signal:** Mentions of "address," "coordinates," "national parks," "universities," or "districts."

### entity-spec-catalogs
**Match when:** The query asks for technical specifications, product attributes, or detailed sub-items belonging to parent brands or programs.
**Load skill:** `decompose-entity-spec-catalogs`
**Key signal:** Requests for "technical specs," "model numbers," "course modules," or "brand portfolios."

## Default Fallback
If no type matches clearly, use general decomposition principles:
- Split by entity or category
- Keep each worker under 30 rows
- List all required columns in every subtask