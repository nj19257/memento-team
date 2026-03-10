---
name: task-router
description: Identifies the task type and directs the orchestrator to the correct decompose skill.
---

## How to Use
1. Read the user's query and identify the primary organizational axis (time, entity, category, or rank).
2. Match it against the task types below.
3. Call `read_orchestrator_skill("decompose-<matched_type>")` to load the specialized strategy.

## Task Types

### split-by-time-period
**Match when:** The query specifies a chronological range — continuous timelines, event logs, or periodic data (e.g., "from 2010 to 2024," "all earthquakes since 2000," "complete match history").
**Load skill:** `decompose-split-by-time-period`
**Key signal:** Date ranges, decades, "year-by-year", event histories, timelines.

### split-by-entity
**Match when:** The query targets specific subjects requiring deep attribute extraction — named entities or category-scoped benchmarking (e.g., "Nikon Z6 vs Sony A7IV," "compare specs of all models in this product line").
**Load skill:** `decompose-split-by-entity`
**Key signal:** Proper nouns, spec sheets, benchmark comparisons, multi-attribute tables.

### split-by-rank-segment
**Match when:** The query involves ordinal rankings — static "Top N" lists or longitudinal annual rankings (e.g., "Top 50 movies," "annual GDP rankings 2010-2024," "yearly box office leaders").
**Load skill:** `decompose-split-by-rank-segment`
**Key signal:** "Top [X]," "ranked," "annual standings," ordinal numbers combined with rankings.

### split-by-category
**Match when:** The query is organized by broad domain classifications, geographic regions, or institutional departments (e.g., "by country," "academic subjects," or "sports leagues").
**Load skill:** `decompose-split-by-category`
**Key signal:** Use of "by [Category Name]" or lists of distinct sectors/regions.

### geographic-registries
**Match when:** The query involves location-based registries or catalogs organized by geographic boundaries (e.g., "all UNESCO sites by country," "hospitals in each province").
**Load skill:** `decompose-geographic-registries`
**Key signal:** Geographic partitioning, "by country/state/region," registry or inventory language.

## Default Fallback
If no type matches clearly, use general decomposition principles:
- Split by entity or category to ensure data independence.
- Keep each worker load under 30 rows to prevent context loss.
- Explicitly list all required columns/attributes in every subtask description.