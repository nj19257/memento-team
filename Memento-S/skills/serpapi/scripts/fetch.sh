#!/usr/bin/env bash
set -euo pipefail

# Fetch a URL and extract readable text content.
# Usage: fetch.sh <url>
#
# Extracts main text from a web page, stripping HTML tags.
# Useful after a search to get full page content from a result URL.

URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "Usage: fetch.sh <url>"
      echo "  Fetches a URL and extracts readable text."
      exit 0
      ;;
    *) URL="$1"; shift ;;
  esac
done

if [[ -z "$URL" ]]; then
  echo "Error: URL is required" >&2
  echo "Usage: fetch.sh <url>" >&2
  exit 1
fi

python3 -c "
import urllib.request, re, sys, html, ssl

url = sys.argv[1]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request(url, headers={
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})

try:
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        raw = resp.read()
        # Try UTF-8 first, fall back to latin-1
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
except Exception as e:
    print(f'Fetch error: {e}', file=sys.stderr)
    sys.exit(1)

# Remove script/style blocks
text = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
# Remove HTML tags
text = re.sub(r'<[^>]+>', ' ', text)
# Decode HTML entities
text = html.unescape(text)
# Collapse whitespace
text = re.sub(r'[ \t]+', ' ', text)
text = re.sub(r'\n\s*\n', '\n\n', text)
text = text.strip()

print(text)
" "$URL"
