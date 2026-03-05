---
name: decompose-temporal-event-logs
description: Auto-generated skill.
---

name: decompose-temporal-event-logs
description: Specialized decomposition strategy for high-volume chronological datasets tracking specific occurrences over long time horizons.

## When to Use
Use this strategy when the query requires a "complete list," "every single," or "detailed dataset" of events tied to a specific entity (e.g., Taylor Swift, Michael Phelps, NASA) over a multi-year or multi-decade span. Indicators include unique date-based columns (`date`, `launchdate`, `time`) and high expected row counts (50+ rows).

## Decomposition Template
1.  **Timeline Segmentation:** Divide the total duration into logical "Eras" or "Cycles" rather than arbitrary chunks.
    *   *Example (ws_en_007):* Instead of 1960-1975, split by program: Worker 1 (Mercury/Gemini), Worker 2 (Apollo), Worker 3 (Skylab).
    *   *Example (ws_en_016):* Split by Olympic Cycles: Worker 1 (2000-2004), Worker 2 (2005-2008), Worker 3 (2009-2012), Worker 4 (2013-2016).
2.  **Source-Specific Verification:** Assign a "Verification Worker" to cross-reference official databases (FIS Biography for Eileen Gu, NASA Mission Logs for spaceflights) against general sources like Wikipedia.
3.  **Boundary Check:** Explicitly define the start and end dates for each worker to prevent "The Gap" error (e.g., Worker 1 ends Dec 31, Worker 2 starts Jan 1).

## Worker Assignment Rules
*   **Max Rows per Worker:** 40-50 rows. If a tour (ws_en_006) has 150 dates, use at least 3 workers.
*   **Worker 1:** Early career/Phase 1 + Schema Definition (defines the table headers).
*   **Worker 2-N:** Middle phases.
*   **Final Worker:** Most recent phase + Deduplication/Chronological Sorting.
*   **Overlap:** Mandate a 1-month overlap between workers to ensure no events are missed during handoffs.

## Required Columns Checklist
*   **The "Context" Column:** Often missed (e.g., `discipline` for Eileen Gu or `missiontype` for NASA).
*   **The "Result" Column:** Ensure `standing`, `result`, or `missionstatus` is captured for every row.
*   **The "Competitor" Column:** For sports (ws_en_003, ws_en_016), the `top3players` or `silver/bronze` winners are frequently omitted; workers must search specifically for podium results.
*   **The "Location" Column:** Ensure `hostvenue` and `hostcity` are distinct (ws_en_006).

## Anti-Patterns
*   **The "Recent Bias" Overlap:** Do not assign three workers to the last two years (as seen in ws_en_003) while leaving early years to a single worker; this causes redundancy and missing early-career data.
*   **Ignoring Non-Major Events:** For athletes, failing to search for "World Cup stops" or "Nor Am Cup" (ws_en_003) leads to massive missing counts (e.g., 58 missing rows).
*   **Date Format Inconsistency:** Allowing workers to use different date formats (e.g., "4th Feb 2010" vs "2010-02-04") which breaks the final sort.
*   **Missing "No-Medal" Results:** In ws_en_016, workers often skip events where the athlete didn't win gold. If the query asks for "achievements" or "results," all finals must be included.