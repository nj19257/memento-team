---
name: decompose-ranked-authority-lists
description: Specialized decomposition strategy for ranked-authority-lists tasks, focusing on year-over-year consistency and rank-segment partitioning.
---

## When to Use
Use this strategy when the query requires data from official annual rankings (QS, THE, NMHC), award registries (CNN Heroes), or statistical reports (ACI Airport Traffic). Indicators include a fixed N (Top 10, Top 50), a specific year (2024, 2025), and a requirement for secondary attributes like "applicationfee", "totalpassengers", or "dateofbirth".

## Decomposition Template
1.  **Identify the Authority Source & Year:** Determine the primary source (e.g., "QS World University Rankings 2025").
2.  **Partition by Primary Key:** 
    *   **For Multi-Year Lists (ws_en_012, ws_en_014):** Partition by Year. Assign one worker per 3-5 year block.
    *   **For Large Single-Year Lists (ws_en_015):** Partition by Rank Segments. Assign one worker per 10-15 ranks (e.g., Ranks 1-25, 26-50).
    *   **For Categorical Rankings (ws_en_001):** Partition by Subject/Category. Assign one worker per broad subject (e.g., Arts & Humanities).
3.  **Attribute Extraction:** Define a standardized schema for all workers to ensure columns like `applicationdeadline` or `totalpassengers` are formatted identically for merging.

## Worker Assignment Rules
*   **Max Rows per Worker:** 10-15 rows if deep research is required (e.g., finding `applicationfee` or `dateofbirth`); 25 rows if only extracting from a single table.
*   **Overlap Prevention:** Explicitly define the range for each worker (e.g., "Worker 1: Years 2020-2022", "Worker 2: Years 2023-2024").
*   **Subject Specialization:** In tasks like **ws_en_001**, assign workers based on the "Broad Subject" to prevent confusion between different ranking tables on the same site.

## Required Columns Checklist
*   **Rank/Year Integrity:** `rank`, `yearofaward`, or `year`.
*   **Entity Identifiers:** `university`, `fullname`, `airport`, `companyname`.
*   **Technical Specs:** `code(iata/icao)`, `numberofmanagedunitsin2024`.
*   **Deep Research Fields:** `applicationfee`, `homepage`, `dateofbirth`, `location` (city, district, road). *Note: These often require visiting secondary pages beyond the main ranking list.*

## Anti-Patterns
*   **The "Missing Subject" Trap:** In **ws_en_001**, failing to create a worker for each of the five broad subjects led to a 100% missing row rate in early iterations. Do not assume one worker can handle multiple distinct ranking tables.
*   **Granularity Mismatch:** For **ws_en_014**, providing only the city for `location` when the prompt asks for "city, district, road" is a failure.
*   **Year Confusion:** Mixing "Year of Award" with "Year of Data" (e.g., NMHC 2025 rankings are based on 2024 units). Workers must be instructed to distinguish between the ranking year and the data year.
*   **Single-Worker Overload:** Assigning a "Top 50" list to a single worker often results in truncation or "hallucinated" summaries. Always split lists >20 items.