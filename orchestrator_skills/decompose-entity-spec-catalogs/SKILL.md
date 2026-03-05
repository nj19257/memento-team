---
name: decompose-entity-spec-catalogs
description: Specialized decomposition strategy for entity-spec-catalogs tasks requiring high-density attribute extraction across multiple parent entities.
---

## When to Use
Use this strategy when the query requires a comprehensive "census" of items belonging to specific parent categories (Brands, University Departments, Artists, or Product Series). Indicators include requests for "all models," "full portfolio," or "complete list of songs," especially when paired with technical specifications like `abv%`, `cpu_model`, `tuition_fees`, or `battery_capacity`.

## Decomposition Template
1.  **Identify Parent Entities**: Break the query into its primary subjects (e.g., in `ws_en_002`, the brands Johnnie Walker, Chivas, etc.; in `ws_en_013`, the specific studio albums).
2.  **Define "Core" vs. "Noise"**: Explicitly define the inclusion/exclusion criteria in the subtask (e.g., "Standard Range only, exclude flavors" for spirits; "U.S. Market only" for `ws_en_004`).
3.  **Attribute Mapping**: List every required column from the `required_columns` field in the subtask prompt to prevent workers from skipping "minor" specs like `packsize` or `ieltsscorerequirement`.
4.  **Temporal Check**: If the query specifies a future or specific date (e.g., "June 2025"), instruct workers to search for "roadmap," "upcoming," or "announced" items to simulate that snapshot.

## Worker Assignment Rules
*   **One Worker per Parent Entity**: For brands or departments (e.g., one worker for "Goldsmiths Media Dept" in `ws_en_009`).
*   **Volume Capping**: Limit each worker to a maximum of 20-25 entities. If a brand has 50+ products, split by sub-category (e.g., Worker A: Samsung S Series, Worker B: Samsung Note Series).
*   **Redundancy**: For high-stakes specs (like `launchprice`), assign a "Verifier" worker to cross-check the numerical data found by the primary collectors.

## Required Columns Checklist
Commonly missed attributes that require explicit callouts:
*   **Technical Specs**: `cpumanufacturingprocess`, `resolution`, `abv%`.
*   **Financials**: `annualinternationaltutionfees`, `launchprice(usd)`.
*   **Metadata**: `compulsorymoduletitles`, `majortrims`, `releaseyear`.
*   **Normalization**: Ensure `packsize` always includes units (e.g., 750ml) and `scores` are clearly labeled (e.g., "out of 100").

## Anti-Patterns
*   **The "Et Cetera" Trap**: Do not allow workers to provide "Top 5" or "Examples." In `ws_en_002`, workers missed 32 rows because they stopped after the most famous labels. Demand the *full* permanent range.
*   **Column Merging**: Do not combine `category` and `sub-category` into one column. Keep them distinct as per the `eval_columns` requirements.
*   **Ignoring Negative Constraints**: In `ws_en_013`, failing to filter for "Standard Edition" leads to duplicate song entries from Deluxe versions.
*   **Single-Source Bias**: Relying only on a brand's homepage often misses "permanent" items sold in specific regions. Workers must use `bash_tool` to check 3rd party retail or academic catalogs.