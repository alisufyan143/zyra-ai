"""
Input normalization and validation layer.
Every piece of data entering the pipeline goes through strict validation.
"""

import re
from typing import Optional
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode

from pydantic import BaseModel, field_validator, model_validator


# ── Tracking params to strip from discovered URLs ──
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "source",
}

# ── URL path segments that indicate auth walls ──
AUTH_INDICATORS = {
    "login", "signin", "sign-in", "sso", "cas", "auth", "saml",
    "oauth", "authenticate", "account", "myportal", "my-portal",
}

# ── File extensions to reject ──
BINARY_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".wav",
    ".zip", ".rar", ".gz", ".tar", ".7z",
    ".css", ".js", ".xml", ".json", ".rss",
}


class DomainInput(BaseModel):
    """Validates and normalizes a raw domain/URL string from the user."""
    raw_url: str

    @field_validator("raw_url")
    @classmethod
    def validate_and_normalize(cls, v: str) -> str:
        # 1. Strip whitespace
        v = v.strip()

        if not v:
            raise ValueError("URL cannot be empty")

        # 2. Add scheme if missing
        if not v.startswith(("http://", "https://")):
            v = "https://" + v

        # 3. Parse and validate
        parsed = urlparse(v)

        # Reject non-http schemes
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid scheme '{parsed.scheme}' -- only http/https allowed")

        # Reject IPs
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("No hostname found in URL")

        # Check for IP addresses
        ip_pattern = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
        if ip_pattern.match(hostname):
            raise ValueError(f"IP addresses not allowed: {hostname}")

        # Reject localhost
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
            raise ValueError("Localhost not allowed")

        # Must have a dot (real domain)
        if "." not in hostname:
            raise ValueError(f"Invalid domain: {hostname}")

        # 4. Upgrade http to https
        if parsed.scheme == "http":
            parsed = parsed._replace(scheme="https")

        # 5. Add www if bare domain (e.g., bucknell.edu -> www.bucknell.edu)
        if hostname.count(".") == 1:
            hostname = "www." + hostname
            parsed = parsed._replace(netloc=hostname)

        # 6. Ensure trailing slash on path
        path = parsed.path
        if not path or path == "":
            path = "/"
        if not path.endswith("/"):
            path = path + "/"
        parsed = parsed._replace(path=path)

        # 7. Strip fragments and query params
        parsed = parsed._replace(fragment="", query="")

        # 8. Lowercase the domain
        parsed = parsed._replace(netloc=parsed.netloc.lower())

        return urlunparse(parsed)


class DiscoveredURL(BaseModel):
    """Validates every URL discovered during crawling."""
    url: str
    source_page: str
    anchor_text: Optional[str] = None
    depth: int = 0

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty")
        return v

    @model_validator(mode="after")
    def normalize_url(self):
        url = self.url
        source = self.source_page

        # 1. Resolve relative URLs against source page
        if not url.startswith(("http://", "https://")):
            url = urljoin(source, url)

        parsed = urlparse(url)

        # 2. Reject non-http schemes (mailto:, tel:, javascript:, data:)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid scheme: {parsed.scheme}")

        # 3. Reject binary file extensions
        path_lower = parsed.path.lower()
        for ext in BINARY_EXTENSIONS:
            if path_lower.endswith(ext):
                raise ValueError(f"Binary file extension: {ext}")

        # 4. Reject auth-wall URLs
        path_segments = set(parsed.path.lower().strip("/").split("/"))
        auth_match = path_segments & AUTH_INDICATORS
        if auth_match:
            raise ValueError(f"Auth-wall URL detected: {auth_match}")

        # 5. Strip fragments
        parsed = parsed._replace(fragment="")

        # 6. Strip tracking params
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
            parsed = parsed._replace(query=urlencode(filtered, doseq=True))

        # 7. Lowercase domain
        parsed = parsed._replace(netloc=parsed.netloc.lower())

        # 8. Collapse duplicate slashes in path
        clean_path = re.sub(r"/+", "/", parsed.path)
        parsed = parsed._replace(path=clean_path)

        self.url = urlunparse(parsed)

        # 9. Check same-domain (including subdomains)
        source_parsed = urlparse(source)
        source_domain = source_parsed.hostname or ""
        url_domain = parsed.hostname or ""

        # Extract base domain (e.g., "bucknell.edu" from "www.bucknell.edu")
        source_base = ".".join(source_domain.split(".")[-2:])
        url_base = ".".join(url_domain.split(".")[-2:])

        if source_base != url_base:
            raise ValueError(f"Off-domain URL: {url_domain} (expected {source_domain})")

        return self


class CrawledPage(BaseModel):
    """Validates every page fetched by the crawler."""
    url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    title: Optional[str] = None
    fetch_time_ms: int = 0
    depth: int = 0

    @field_validator("content_type")
    @classmethod
    def must_be_html(cls, v: str) -> str:
        if "text/html" not in v.lower():
            raise ValueError(f"Not HTML content: {v}")
        return v

    @field_validator("status_code")
    @classmethod
    def must_be_success(cls, v: int) -> int:
        if v >= 400:
            raise ValueError(f"HTTP error status: {v}")
        return v

    @field_validator("html")
    @classmethod
    def not_too_thin(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 100:
            raise ValueError(f"Page content too thin ({len(stripped)} chars)")
        return v
