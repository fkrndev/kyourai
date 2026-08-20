"""Link understanding — URL extraction with SSRF protection.

Inspired by OpenClaw's link-understanding module. Provides:
  - URL extraction from text
  - SSRF (Server-Side Request Forgery) protection
  - Safe URL fetching with timeout and size limits
  - Content extraction from HTML

Usage:
    from kyourai.tools.link_understanding import extract_urls, safe_fetch_url

    urls = extract_urls("Check out https://example.com for details")
    # → ["https://example.com"]

    content = safe_fetch_url("https://example.com")
    # → "Example Domain\n\nThis domain is for use in..."
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

# URL regex — matches http(s) URLs
URL_PATTERN = re.compile(
    r"https?://[^\s<>\[\]{}\"'\\]+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Extract URLs from text.

    Args:
        text: Text to extract URLs from

    Returns:
        List of URLs found in the text (deduplicated, order preserved)
    """
    if not text:
        return []

    urls = URL_PATTERN.findall(text)

    # Clean trailing punctuation
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls:
        # Strip trailing punctuation
        url = url.rstrip(".,;:!?)")
        # Normalize
        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------


# Blocked IP ranges (private, loopback, link-local, etc.)
BLOCKED_IP_RANGES = [
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    # Private
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    # Link-local
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # Reserved
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    # Multicast
    ipaddress.ip_network("224.0.0.0/4"),
    # Broadcast
    ipaddress.ip_network("255.255.255.255/32"),
]

# Blocked hostnames
BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",  # GCP metadata
    "169.254.169.254",  # Cloud metadata endpoints
    "metadata.aws.internal",
    "metadata.azure.com",
}

# Allowed schemes
ALLOWED_SCHEMES = {"http", "https"}


@dataclass(slots=True)
class SSRFCheckResult:
    """Result of SSRF validation."""
    is_safe: bool
    url: str = ""
    reason: str = ""
    resolved_ip: str = ""


def check_ssrf(url: str) -> SSRFCheckResult:
    """Check if a URL is safe from SSRF attacks.

    Args:
        url: URL to check

    Returns:
        SSRFCheckResult with safety status
    """
    try:
        parsed = urlparse(url)

        # Check scheme
        if parsed.scheme not in ALLOWED_SCHEMES:
            return SSRFCheckResult(
                is_safe=False,
                url=url,
                reason=f"Scheme '{parsed.scheme}' not allowed",
            )

        hostname = parsed.hostname or ""
        if not hostname:
            return SSRFCheckResult(
                is_safe=False,
                url=url,
                reason="No hostname in URL",
            )

        # Check blocked hostnames
        if hostname.lower() in BLOCKED_HOSTS:
            return SSRFCheckResult(
                is_safe=False,
                url=url,
                reason=f"Blocked hostname: {hostname}",
            )

        # Check if hostname is an IP
        try:
            ip = ipaddress.ip_address(hostname)
            if _is_blocked_ip(ip):
                return SSRFCheckResult(
                    is_safe=False,
                    url=url,
                    reason=f"Blocked IP range: {ip}",
                    resolved_ip=str(ip),
                )
        except ValueError:
            # Not an IP — resolve DNS
            try:
                # Use getaddrinfo to resolve
                addrs = socket.getaddrinfo(hostname, None)
                for addr in addrs:
                    ip_str = addr[4][0]
                    try:
                        ip = ipaddress.ip_address(ip_str)
                        if _is_blocked_ip(ip):
                            return SSRFCheckResult(
                                is_safe=False,
                                url=url,
                                reason=f"Hostname resolves to blocked IP: {ip}",
                                resolved_ip=str(ip),
                            )
                    except ValueError:
                        continue
            except socket.gaierror:
                return SSRFCheckResult(
                    is_safe=False,
                    url=url,
                    reason=f"DNS resolution failed for: {hostname}",
                )

        return SSRFCheckResult(is_safe=True, url=url)

    except Exception as e:
        return SSRFCheckResult(
            is_safe=False,
            url=url,
            reason=f"URL parsing error: {e}",
        )


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is in a blocked range."""
    for network in BLOCKED_IP_RANGES:
        if ip in network:
            return True
    return False


# ---------------------------------------------------------------------------
# Safe URL fetching
# ---------------------------------------------------------------------------

MAX_FETCH_SIZE = 1_000_000  # 1MB max content
FETCH_TIMEOUT = 15  # seconds
MAX_CONTENT_LENGTH = 100_000  # 100KB of text content


@dataclass(slots=True)
class FetchResult:
    """Result of fetching a URL."""
    url: str
    success: bool
    content: str = ""
    content_type: str = ""
    status_code: int = 0
    error: str = ""
    final_url: str = ""
    fetched_at: float = 0.0


def safe_fetch_url(
    url: str,
    *,
    timeout: int = FETCH_TIMEOUT,
    max_size: int = MAX_FETCH_SIZE,
) -> FetchResult:
    """Safely fetch content from a URL with SSRF protection.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        max_size: Maximum response size in bytes

    Returns:
        FetchResult with the content or error
    """
    import time

    # SSRF check
    ssrf_result = check_ssrf(url)
    if not ssrf_result.is_safe:
        return FetchResult(
            url=url,
            success=False,
            error=f"SSRF blocked: {ssrf_result.reason}",
        )

    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Kyourai/1.0 (AI Agent)",
                "Accept": "text/html,application/json,text/plain,*/*",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            status = response.status

            # Check content length
            content_length = int(response.headers.get("Content-Length", 0))
            if content_length > max_size:
                return FetchResult(
                    url=url,
                    success=False,
                    error=f"Content too large: {content_length} bytes (max {max_size})",
                    status_code=status,
                    content_type=content_type,
                )

            # Read content (with size limit)
            raw = response.read(max_size + 1)
            if len(raw) > max_size:
                return FetchResult(
                    url=url,
                    success=False,
                    error=f"Content exceeds size limit: {max_size} bytes",
                    status_code=status,
                    content_type=content_type,
                )

            final_url = response.url

            # Extract text content
            if "html" in content_type.lower():
                text = extract_text_from_html(raw.decode("utf-8", errors="replace"))
            elif "json" in content_type.lower():
                text = raw.decode("utf-8", errors="replace")
            else:
                text = raw.decode("utf-8", errors="replace")

            # Truncate if too long
            if len(text) > MAX_CONTENT_LENGTH:
                text = text[:MAX_CONTENT_LENGTH] + f"\n...[truncated {len(text) - MAX_CONTENT_LENGTH} chars]"

            return FetchResult(
                url=url,
                success=True,
                content=text,
                content_type=content_type,
                status_code=status,
                final_url=final_url,
                fetched_at=time.time(),
            )

    except urllib.error.HTTPError as e:
        return FetchResult(
            url=url,
            success=False,
            error=f"HTTP {e.code}: {e.reason}",
            status_code=e.code,
        )
    except urllib.error.URLError as e:
        return FetchResult(
            url=url,
            success=False,
            error=f"URL error: {e.reason}",
        )
    except Exception as e:
        return FetchResult(
            url=url,
            success=False,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

# Tags to remove (scripts, styles, etc.)
REMOVE_TAGS = re.compile(
    r"<(script|style|noscript|iframe|svg|math)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# All HTML tags
ALL_TAGS = re.compile(r"<[^>]+>")

# HTML entities
HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
    "&hellip;": "...",
    "&mdash;": "—",
    "&ndash;": "–",
    "&copy;": "©",
    "&trade;": "™",
}


def extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML.

    Removes scripts, styles, and tags. Preserves text content.
    """
    # Remove script/style sections
    text = REMOVE_TAGS.sub("", html)

    # Convert <br>, <p>, <div> to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)

    # Remove all remaining tags
    text = ALL_TAGS.sub("", text)

    # Decode HTML entities
    for entity, char in HTML_ENTITIES.items():
        text = text.replace(entity, char)

    # Decode numeric entities
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Link processing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LinkProcessingResult:
    """Result of processing links in a message."""
    links_found: list[str] = field(default_factory=list)
    links_fetched: list[FetchResult] = field(default_factory=list)
    links_blocked: list[str] = field(default_factory=list)
    extracted_content: str = ""


def process_links_in_text(
    text: str,
    *,
    fetch: bool = True,
    timeout: int = FETCH_TIMEOUT,
) -> LinkProcessingResult:
    """Process all links in a text message.

    Args:
        text: Text containing links
        fetch: Whether to fetch link content
        timeout: Fetch timeout per link

    Returns:
        LinkProcessingResult with all findings
    """
    result = LinkProcessingResult()
    result.links_found = extract_urls(text)

    if not fetch:
        return result

    content_parts: list[str] = []

    for url in result.links_found:
        fetch_result = safe_fetch_url(url, timeout=timeout)
        result.links_fetched.append(fetch_result)

        if not fetch_result.success:
            if "SSRF" in fetch_result.error:
                result.links_blocked.append(url)
            continue

        # Add extracted content
        content_parts.append(
            f"[Link: {fetch_result.final_url or url}]\n{fetch_result.content[:5000]}\n"
        )

    result.extracted_content = "\n---\n".join(content_parts)
    return result
