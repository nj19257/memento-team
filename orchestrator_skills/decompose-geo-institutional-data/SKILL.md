---
name: decompose-geo-institutional-data
description: Specialized decomposition strategy for geo-institutional-data tasks involving physical locations, administrative bodies, and official designations.
---

## When to Use
Use this strategy when the query requires a comprehensive list of entities defined by geographic boundaries (e.g., UK, North America) or institutional affiliations (e.g., Ivy League, Group of Eight). It is particularly effective for tasks requiring official metadata like designation years, responsible public bodies, or specific admission requirements.

## Decomposition Template
1.  **Geographic/Institutional Partitioning**: Divide the total scope into mutually exclusive subsets based on region or alliance.
    *   *Example (ws_en_005)*: Split by "Ivy League (USA)" and "Group of Eight (Australia)".
    *   *Example (ws_en_020)*: Split North America into "USA", "Canada", and "Mexico".
2.  **Entity Enumeration**: First, generate a master list of entity names for each partition to prevent missing rows (e.g., list all 86 UK scenic areas before searching for their websites).
3.  **Attribute Extraction**: For each entity, search for the specific required columns (e.g., "2023 total visitor spending" for ws_en_017 or "designation category" for ws_en_010).
4.  **Standardization**: Convert local metrics (e.g., Australian WAM vs. US GPA) into the format requested by the user.

## Worker Assignment Rules
*   **Max Rows Per Worker**: 15-20 rows. For large datasets like ws_en_010 (86 rows), assign at least 5 workers.
*   **Worker 1**: Region A (e.g., England AONBs).
*   **Worker 2**: Region B (e.g., Scotland NSAs & Wales AONBs).
*   **Worker 3**: Region C (e.g., Northern Ireland AONBs).
*   **Consolidator**: Merges tables and ensures "NA" is used for missing values as per ws_en_020.

## Required Columns Checklist
*   **Official Identifiers**: `responsiblepublicbody`, `designationcategory`, `alliance`.
*   **Location Metadata**: `counties`, `state(s)`, `mailingaddress`.
*   **Temporal/Quantitative Data**: `designationtime`, `yearofrecognition`, `2023visitorsnumber(million)`.
*   **Verification Links**: `website`. Ensure every row has a source or official URL.

## Anti-Patterns
*   **The "Missing Tail" Error**: Do not assign more than 20 entities to a single worker. In ws_en_010, workers missed 15 rows because the list was too long for a single pass.
*   **Vague Program Matching**: In ws_en_005, workers pulled "Electrical Engineering" data for a "Civil Engineering" request. Workers must verify the specific department/major.
*   **Ignoring Multi-Jurisdiction Sites**: For ws_en_017, ensure sites spanning multiple states (e.g., Great Smoky Mountains) are not skipped or assigned to only one state worker without coordination.
*   **Metric Confusion**: Failing to distinguish between "Total Visitor Spending" and "Visitor Numbers" (ws_en_017). Always check units (Millions vs. Absolute).