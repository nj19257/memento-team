---
name: serpapi
description: Search Google via SerpAPI (Google Search, Google News, Google Local) and fetch web page content. Use when you need to search the web, find news articles, look up local businesses, or extract content from URLs. Supports country/language targeting for region-specific results.
metadata: {"clawdbot":{"emoji":"🔍","requires":{"bins":["curl","python3"],"env":["SERPAPI_API_KEY"]},"primaryEnv":"SERPAPI_API_KEY"}}
---

# SerpAPI Search & Fetch

Search Google via SerpAPI and fetch full web page content.

## Tools

### 1. `search.sh` — Web search
Returns search result titles, URLs, and snippets.

```bash
{baseDir}/scripts/search.sh "query here" --num 5
```

### 2. `fetch.sh` — Fetch URL content
Fetches a URL and extracts readable text. Use this after search to get full page content.

```bash
{baseDir}/scripts/fetch.sh "https://example.com/article" --max-chars 8000
```

## Best practice: Search then Fetch

1. **Search once** with a focused query to find the right URL
2. **Fetch the URL** to get full content — don't repeat searches for missing details

```bash
# Step 1: Search to find the right page
{baseDir}/scripts/search.sh "Forbes billionaires 2024 top 10" --num 5

# Step 2: Fetch the page content for complete data
{baseDir}/scripts/fetch.sh "https://www.forbes.com/sites/.../the-top-200/"
```

## Search engines

| Engine | Use case | Flag |
|--------|----------|------|
| `google` | Web search (default) | `--engine google` |
| `google_news` | News articles | `--engine google_news` |
| `google_local` | Local businesses/places | `--engine google_local` |

## Search options

| Flag | Description | Default |
|------|-------------|---------|
| `--engine` | `google`, `google_news`, `google_local` | `google` |
| `--country` | 2-letter country code (`br`, `us`, `de`, etc.) | `us` |
| `--lang` | Language code (`pt`, `en`, `es`, etc.) | `en` |
| `--location` | Location string (e.g. `"São Paulo, Brazil"`) | — |
| `--num` | Number of results | `10` |
| `--json` | Raw JSON output | off |

## Fetch options

| Flag | Description | Default |
|------|-------------|---------|
| `--max-chars` | Max characters to return | `8000` |

## API key

Set `SERPAPI_API_KEY` env var, or store it:
```bash
mkdir -p ~/.config/serpapi
echo "your_key_here" > ~/.config/serpapi/api_key
chmod 600 ~/.config/serpapi/api_key
```
