---
name: search-strategy
description: Determines the optimal search strategy for worker subtasks and embeds it into subtask descriptions.
---

## When to Use
Read this skill AFTER choosing a decompose strategy, BEFORE writing subtask descriptions.
Different data sources require fundamentally different search approaches. Choosing the wrong one leads to low recall (missing data) or wasted tool calls.

## Strategy Selection

Evaluate the data source and pick ONE strategy per subtask:

### 1. Index Enumeration (structured sources)
**Use when:** The data lives in a known, browsable index — journal TOCs, product catalog pages, official registries, league season schedules, conference proceedings.
**How it works:** Fetch the index/TOC page → scan all entries → filter locally by criteria.
**Why:** Exhaustive by design. One fetch per index page covers all items in that scope.

**Signal words:** "all articles in [journal]", "every product in [catalog]", "complete list from [official source]"

**Subtask hint to include:**
```
Search Strategy: INDEX ENUMERATION
1. Find the index/TOC page for [source] (e.g., volumes-and-issues page, product listing page, season schedule page).
2. Fetch each index page covering your assigned range.
3. Scan all entries and filter by the criteria below.
4. Do NOT use Google keyword search as your primary method — it will miss entries not indexed by Google.
```

### 2. Keyword Search (scattered sources)
**Use when:** No single authoritative index exists. Data is spread across many websites — news articles, product reviews, general facts, biographical info.
**How it works:** Google search with targeted keywords → follow top results → extract data.
**Why:** The data has no central listing, so search engines are the only discovery mechanism.

**Signal words:** "find information about", "what are the specs of", "compare [things from different sources]"

**Subtask hint to include:**
```
Search Strategy: KEYWORD SEARCH
Use web search with varied keyword combinations. Try at least 3 different query formulations.
Cross-reference 2+ sources for each data point.
```

### 3. Hybrid (index + keyword fill)
**Use when:** A primary index exists but may be incomplete, or metadata (like page numbers, authors) requires supplemental searches.
**How it works:** Start with index enumeration for discovery, then use keyword search to fill gaps.
**Why:** Gets the exhaustive coverage of index enumeration with the detail of keyword search.

**Subtask hint to include:**
```
Search Strategy: HYBRID
1. PRIMARY: Fetch the index/TOC page for [source] to discover all items.
2. SUPPLEMENT: For any missing metadata (authors, dates, page numbers), use targeted keyword searches.
3. Do NOT skip step 1 — keyword search alone will miss items.
```

## Decision Table

| Data characteristic | Strategy | Example |
|---|---|---|
| Single authoritative source with browsable listing | Index Enumeration | Journal articles, official product pages, government registries |
| No central listing, data across many sites | Keyword Search | General facts, cross-source comparisons, news events |
| Central listing exists but metadata is sparse | Hybrid | Journal articles needing author/page details from secondary sources |
| API available (CrossRef, OpenAlex, govt data portals) | Index Enumeration (via API) | Academic metadata, public datasets |

## How to Embed in Subtasks
Add a `Search Strategy` block at the end of each subtask description, BEFORE the format example. The worker will follow this as its primary approach instead of defaulting to Google keyword search for everything.

## Anti-Patterns
- **Google-for-everything:** Using keyword search when an authoritative index exists. Leads to ~30% recall on exhaustive tasks.
- **Fetching individual items:** Fetching each item's detail page one-by-one instead of scanning an index page that lists many items. Wastes tool calls (66 fetches vs 10 TOC pages).
- **Ignoring the source structure:** Not telling workers WHERE to look. Without guidance, workers default to Google, which is wrong for bounded-source tasks.
