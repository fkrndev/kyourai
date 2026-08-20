"""Web search tool — DuckDuckGo HTML search, no API key required.

The agent's web interface. Uses DuckDuckGo's HTML endpoint (no API key,
no rate limits for reasonable use). Returns titles + URLs + snippets.

Hermes uses a provider-based web search (web_search_provider.py +
web_search_registry.py) with multiple backends. Kyourai starts with
DuckDuckGo only — zero config, zero cost. Can be extended later.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300
REQUEST_TIMEOUT = 10  # seconds


def _fetch_html(url: str) -> str:
    """Fetch URL content with a basic urllib request (no external deps)."""
    import urllib.request
    import urllib.error
    import ssl

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    # Some environments (corporate proxies, self-signed certs) fail SSL
    # verification. Create a context that falls back to unverified only
    # if the default context fails.
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except ssl.SSLError:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")


def _parse_ddg_results(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML results page."""
    results: list[dict[str, str]] = []

    # DuckDuckGo HTML results have result blocks in <div class="result ...">
    # Each has <a class="result__a" href="...">title</a>
    # and <a class="result__snippet" ...>snippet</a>

    # Extract result blocks
    blocks = re.split(r'<div class="result ', html)

    for block in blocks[1:]:  # skip the first split (before first result)
        if len(results) >= max_results:
            break

        # Extract title + URL
        title_match = re.search(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not title_match:
            continue

        url_raw = title_match.group(1)
        title_html = title_match.group(2)
        title = re.sub(r"<[^>]+>", "", title_html).strip()

        # DDG uses redirect URLs — extract actual URL
        # Format: //duckduckgo.com/l/?uddg=<encoded_url>&...
        url = url_raw
        if "uddg=" in url:
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if "uddg" in qs:
                url = qs["uddg"][0]

        # Extract snippet
        snippet_match = re.search(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        snippet = ""
        if snippet_match:
            snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[:MAX_SNIPPET_CHARS] + "..."

        if title and url:
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })

    return results


TOOL_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web using DuckDuckGo. Returns up to 5 results with "
        "title, URL, and snippet. No API key required. Use this to find "
        "current information, documentation, or solutions to problems."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return (default 5, max 10)",
            },
        },
        "required": ["query"],
    },
}


def handle(query: str | None = None, max_results: int | None = None, **kwargs) -> str:
    """Search the web and return formatted results.

    Args:
        query: Search query string
        max_results: Max results (default 5, max 10)

    Returns:
        Formatted search results, or error message
    """
    if not query or not isinstance(query, str) or not query.strip():
        return "Error: 'query' parameter is required (must be a non-empty string)"

    if max_results is None:
        max_results = MAX_RESULTS
    max_results = max(1, min(max_results, 10))

    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    try:
        html = _fetch_html(url)
    except Exception as e:
        return f"Web search failed: {e}"

    results = _parse_ddg_results(html, max_results)

    if not results:
        return f"No results found for '{query}'"

    # Format results
    lines = [f"Search results for '{query}' ({len(results)} found):\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
        lines.append("")

    return "\n".join(lines)
