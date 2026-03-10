---
name: decompose-split-by-rank-segment
description: Specialized decomposition strategy for split-by-rank-segment tasks.
---

## When to Use
Use this strategy when data is organized by ordinal position or periodic rankings. Covers both:
- **Static rankings** (e.g., "Top 50 movies", "100 best-selling albums") — single snapshot
- **Longitudinal rankings** (e.g., "annual GDP rankings 2010-2024", "yearly box office leaders") — repeated snapshots over time
Indicated by "Top N", "ranked", "annual standings", or queries combining rankings with time series.

## Decomposition Template
1. **Identify the Primary Pivot:** Determine if the data is organized by rank (Top 1-50), by time (Year X), or both (Top 10 per year). Identify the authoritative source.
2. **Segment by Pivot:** For rank-only: divide into segments of 25-50. For rank×time: assign one worker per year (or 1-3 years). Distinguish "Static Attributes" (entity name, origin) from "Variable Metrics" (value/rank in a given period).
3. **Define Attribute Extraction Requirements:** Specify primary entity name and all secondary metrics or metadata required.
4. **Synthesize and Re-order:** Consolidate into a single structure, ensuring ordinal integrity (1 to N) and chronological order are preserved.

## Worker Assignment Rules
- **Partitioning:** For rank-only tasks: one worker per 25-50 rows. For rank×time tasks: one worker per 1-3 years. **Always prefer more workers with narrower scope.**
- **Overlap Prevention:** Ensure segment boundaries are explicit (e.g., Worker 1: Ranks 1-25; Worker 2: Ranks 26-50) to avoid duplicate entries.
- **Re-ranking Awareness:** Each time period is a fresh ranking — do not assume an entity's rank is static across years.
- **Verification:** If the ranking has multiple versions (e.g., "Preliminary" vs "Final"), assign a verification worker.

## Required Columns Checklist
- **Ordinal Identifier:** The specific rank or position number (essential for maintaining sequence).
- **Primary Entity Name:** The name of the individual, company, or object being ranked.
- **Quantitative Metrics:** The specific values that determined the ranking (e.g., volume, revenue, score).
- **Temporal Metadata:** Dates related to the entity's history or the data collection period (e.g., founding year, date of measurement).
- **Categorical Attributes:** Descriptive traits required for the final output (e.g., location, type, classification).

## Anti-Patterns
- **The "Missing Middle" Error:** Failing to define explicit start/end points for segments, leading to gaps in the sequence (e.g., skipping ranks 25-26).
- **Attribute Drift:** Workers in different segments extracting different types of data for the same column (e.g., one worker providing "Year Founded" while another provides "Age").
- **Source Mismatch:** Using a different version of a list for different segments (e.g., using the 2023 list for ranks 1-25 and the 2024 list for ranks 26-50).
- **Unordered Synthesis:** Merging worker outputs without re-sorting, resulting in a table where Rank 26 appears before Rank 1.