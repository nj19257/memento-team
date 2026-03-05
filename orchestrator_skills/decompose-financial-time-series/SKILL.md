---
name: decompose-financial-time-series
description: Specialized decomposition strategy for financial-time-series tasks, focusing on entity-period partitioning to ensure data integrity across fiscal cycles and market exchanges.
---

## When to Use
Use this strategy when the query involves longitudinal financial data (monthly, quarterly, or annual) or cross-sectional performance metrics for specific entities. Indicators include requests for "fiscal years" (ws_en_018), "statistical months" (ws_en_008), or specific corporate cohorts (ws_en_019).

## Decomposition Template
1. **Identify the Primary Pivot**: Determine if the query is driven by Time (e.g., 2015-2024) or Entity (e.g., NASDAQ, NYSE).
2. **Segment by Reporting Cycle**:
   - For Macro/Fiscal data: Group by 2-3 year blocks to manage data density (e.g., Worker 1: 2015-2019; Worker 2: 2020-2024).
   - For Exchange data: Group by specific Exchange Name + Time Range (e.g., Worker 1: Asian Exchanges Jan-May 2025; Worker 2: US Exchanges Jan-May 2025).
3. **Define Metric Extraction**: Explicitly list the required financial units (USD millions, trillions, per share) in the sub-task to avoid unit conversion errors.
4. **Example (ws_en_008)**:
   - Sub-task 1: Retrieve monthly data (Jan-May 2025) for NASDAQ and NYSE.
   - Sub-task 2: Retrieve monthly data (Jan-May 2025) for SSE, SZSE, and HKEX.
5. **Example (ws_en_019)**:
   - Sub-task 1: Retrieve listing and IPO details for CG Oncology, Zenas BioPharma, and Upstream Bio.
   - Sub-task 2: Retrieve listing and IPO details for MBX Biosciences and Metagenomi.

## Worker Assignment Rules
- **Max Rows per Worker**: 10-15 rows (financial data requires high precision and often involves multiple columns like `domesticmarketcapitalization(usdmillions)`).
- **Entity Grouping**: Keep all data for a single entity (e.g., "NASDAQ") within one worker if the time series is short (<12 months) to ensure trend consistency.
- **Parallelization**: Assign one worker per geographic region or per 5-year fiscal block.

## Required Columns Checklist
- **Temporal Identifiers**: `statisticalmonth`, `fiscalyear`, or `listingdate`.
- **Entity Identifiers**: `exchangename`, `companyname`, or `bloombergticker`.
- **Monetary Metrics**: Ensure the specific unit is captured (e.g., `totaltradingvalue(usdmillions)` vs `federalbudget` in trillions).
- **Performance Indicators**: `netincomeattributabletoshareholders(million)`, `r&dexpenses(million)`, or `indexlevels`.

## Anti-Patterns
- **The "All-in-One" Request**: Do not ask a single worker to fetch 10 years of data for 5 different metrics; this leads to truncated results or missing years (e.g., missing 2024 in ws_en_018).
- **Unit Neglect**: Failing to specify "USD millions" in the sub-task, leading to workers providing local currency or unscaled numbers.
- **Missing Tickers**: Forgetting to require `bloombergticker` or `exchangename` in the sub-task, which makes the final table merge difficult.
- **Overlapping Periods**: Assigning "2015-2020" and "2020-2024" to different workers without specifying if 2020 is inclusive/exclusive, leading to duplicate rows.