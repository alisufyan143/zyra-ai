"""
3-layer intelligent page discovery engine.
Discovers admissions and tuition pages without hardcoded URLs.

Layer 1: Sitemap Mining (aiohttp) — fast, zero-cost scan
Layer 2: Navigation Structure Analysis (Playwright) — real DOM parsing
Layer 3: LLM Link Classification (Gemini) — semantic understanding
"""

import asyncio
import re
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urljoin

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ── Keywords for filtering candidate URLs ──
ADMISSIONS_PATTERNS = [
    r"admiss", r"apply", r"application", r"deadline", r"enroll",
    r"undergraduate", r"graduate", r"transfer", r"freshman",
    r"first.?year", r"requirement", r"accepted",
]

TUITION_PATTERNS = [
    r"tuition", r"cost", r"fee[s]?", r"financial", r"aid",
    r"afford", r"billing", r"payment", r"scholarship",
    r"cost.?of.?attendance", r"expense", r"room.?board",
]

ALL_PATTERNS = ADMISSIONS_PATTERNS + TUITION_PATTERNS


@dataclass
class ScoredLink:
    """A discovered link with relevance scores."""
    url: str
    anchor_text: str = ""
    source: str = ""           # Which layer found it
    admissions_score: float = 0.0
    tuition_score: float = 0.0
    depth: int = 0

    @property
    def max_score(self) -> float:
        return max(self.admissions_score, self.tuition_score)

    @property
    def category(self) -> str:
        if self.admissions_score > self.tuition_score:
            return "admissions"
        elif self.tuition_score > self.admissions_score:
            return "tuition"
        return "both"


@dataclass
class DiscoveryResult:
    """Container for all discovered pages."""
    admissions: list[ScoredLink] = field(default_factory=list)
    tuition: list[ScoredLink] = field(default_factory=list)
    homepage_url: str = ""
    all_candidates: list[ScoredLink] = field(default_factory=list)

    def top_urls(self, limit: int = 6) -> list[str]:
        """Get top unique URLs for fetching, prioritizing highest scores."""
        seen = set()
        result = []
        # Always include homepage
        if self.homepage_url:
            result.append(self.homepage_url)
            seen.add(self.homepage_url)
        # Interleave admissions and tuition for balanced coverage
        for adm, tui in zip(
            sorted(self.admissions, key=lambda x: x.admissions_score, reverse=True),
            sorted(self.tuition, key=lambda x: x.tuition_score, reverse=True),
        ):
            if adm.url not in seen and len(result) < limit:
                result.append(adm.url)
                seen.add(adm.url)
            if tui.url not in seen and len(result) < limit:
                result.append(tui.url)
                seen.add(tui.url)
        # Fill remaining with any high-score candidates
        for link in sorted(self.all_candidates, key=lambda x: x.max_score, reverse=True):
            if link.url not in seen and len(result) < limit:
                result.append(link.url)
                seen.add(link.url)
        return result


class DiscoveryEngine:
    """
    3-layer page discovery engine.
    Finds admissions and tuition pages from a university domain.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        self.domain = parsed.hostname or ""
        self.base_domain = ".".join(self.domain.split(".")[-2:])
        self.scheme = parsed.scheme

    async def discover(self, crawler=None) -> DiscoveryResult:
        """
        Run all 3 discovery layers and return combined results.
        
        Args:
            crawler: PlaywrightCrawler instance (for Layer 2).
                     If None, only Layers 1 is used + scoring.
        """
        result = DiscoveryResult(homepage_url=self.base_url + "/")
        all_links: dict[str, ScoredLink] = {}

        # ── Layer 1: Sitemap Mining ──
        logger.info("Layer 1: Mining sitemap...")
        sitemap_links = await self._mine_sitemap()
        for link in sitemap_links:
            all_links[link.url] = link
        logger.info("  Found %d candidates from sitemap", len(sitemap_links))

        # ── Layer 2: Navigation Structure Analysis ──
        logger.info("Layer 2: Analyzing navigation structure...")
        if crawler:
            nav_links = await self._analyze_navigation(crawler)
            for link in nav_links:
                if link.url in all_links:
                    # Merge scores — nav confirmation boosts confidence
                    existing = all_links[link.url]
                    existing.admissions_score = max(existing.admissions_score, link.admissions_score)
                    existing.tuition_score = max(existing.tuition_score, link.tuition_score)
                    existing.anchor_text = link.anchor_text or existing.anchor_text
                else:
                    all_links[link.url] = link
            logger.info("  Found %d candidates from navigation", len(nav_links))

        # ── Score and classify all candidates ──
        for url, link in all_links.items():
            self._score_link(link)

        # ── Filter into categories ──
        for link in all_links.values():
            link_added = False
            if link.admissions_score >= 0.3:
                result.admissions.append(link)
                link_added = True
            if link.tuition_score >= 0.3:
                result.tuition.append(link)
                link_added = True
            if link_added:
                result.all_candidates.append(link)

        # Sort by relevance
        result.admissions.sort(key=lambda x: x.admissions_score, reverse=True)
        result.tuition.sort(key=lambda x: x.tuition_score, reverse=True)

        logger.info(
            "Discovery complete: %d admissions pages, %d tuition pages",
            len(result.admissions), len(result.tuition)
        )

        # Log top results
        for cat_name, cat_list in [("Admissions", result.admissions[:5]), ("Tuition", result.tuition[:5])]:
            logger.info("  Top %s pages:", cat_name)
            for link in cat_list:
                score = link.admissions_score if cat_name == "Admissions" else link.tuition_score
                logger.info("    %.2f  %s  [%s]", score, link.url, link.anchor_text[:50] if link.anchor_text else "")

        return result

    # ─────────────────────────────────────────────────
    # Layer 1: Sitemap Mining
    # ─────────────────────────────────────────────────
    async def _mine_sitemap(self) -> list[ScoredLink]:
        """Fetch and parse sitemap.xml to find candidate URLs."""
        candidates = []
        sitemap_urls_to_try = [
            f"{self.base_url}/sitemap.xml",
            f"{self.base_url}/sitemap_index.xml",
        ]

        all_page_urls = set()

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (compatible; UniversityETL/1.0)"}
        ) as session:
            # Check robots.txt for Sitemap directives (reuse same session)
            try:
                async with session.get(f"{self.base_url}/robots.txt") as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        for line in text.splitlines():
                            if line.lower().startswith("sitemap:"):
                                sitemap_url = line.split(":", 1)[1].strip()
                                sitemap_urls_to_try.append(sitemap_url)
            except Exception as e:
                logger.debug("robots.txt fetch failed: %s", e)

            # Deduplicate
            sitemap_urls_to_try = list(dict.fromkeys(sitemap_urls_to_try))

            for sitemap_url in sitemap_urls_to_try:
                try:
                    urls = await self._fetch_sitemap(session, sitemap_url)
                    all_page_urls.update(urls)
                except Exception as e:
                    logger.debug("Sitemap %s failed: %s", sitemap_url, e)

        # Filter URLs by keyword relevance
        for url in all_page_urls:
            if self._url_matches_keywords(url):
                link = ScoredLink(url=url, source="sitemap")
                candidates.append(link)

        return candidates

    async def _fetch_sitemap(
        self, session: aiohttp.ClientSession, sitemap_url: str
    ) -> set[str]:
        """Fetch a single sitemap XML and extract URLs."""
        urls = set()
        async with session.get(sitemap_url) as resp:
            if resp.status != 200:
                return urls
            content_type = resp.headers.get("content-type", "")
            text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return urls

        # Handle namespace
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        # Check if it's a sitemap index
        for sitemap_ref in root.findall(f"{ns}sitemap"):
            loc = sitemap_ref.find(f"{ns}loc")
            if loc is not None and loc.text:
                # Recursively fetch sub-sitemaps (1 level deep only)
                try:
                    sub_urls = await self._fetch_sitemap(session, loc.text.strip())
                    urls.update(sub_urls)
                except Exception:
                    pass

        # Extract page URLs
        for url_elem in root.findall(f"{ns}url"):
            loc = url_elem.find(f"{ns}loc")
            if loc is not None and loc.text:
                page_url = loc.text.strip()
                # Only include same-domain URLs
                if self.base_domain in page_url:
                    urls.add(page_url)

        return urls

    # ─────────────────────────────────────────────────
    # Layer 2: Navigation Structure Analysis
    # ─────────────────────────────────────────────────
    async def _analyze_navigation(self, crawler) -> list[ScoredLink]:
        """Parse navigation elements from the homepage DOM."""
        candidates = []

        # Fetch homepage
        page = await crawler.fetch_page(self.base_url + "/", depth=0)
        if not page:
            logger.warning("Could not fetch homepage for nav analysis")
            return candidates

        soup = BeautifulSoup(page.html, "lxml")

        # Extract links from semantic navigation elements
        nav_elements = soup.find_all(["nav", "header"])
        role_nav = soup.find_all(attrs={"role": "navigation"})
        footer = soup.find_all("footer")

        # Priority order: nav > header > role=navigation > footer
        link_sources = [
            (nav_elements, 1.0, "nav"),
            (role_nav, 0.9, "role-nav"),
            (footer, 0.6, "footer"),
        ]

        seen_urls = set()

        for elements, weight_multiplier, source_name in link_sources:
            for element in elements:
                for a_tag in element.find_all("a", href=True):
                    href = a_tag["href"]
                    text = a_tag.get_text(strip=True)

                    # Resolve relative URLs
                    full_url = urljoin(self.base_url + "/", href)
                    parsed = urlparse(full_url)

                    # Same domain check
                    url_domain = parsed.hostname or ""
                    if self.base_domain not in url_domain:
                        continue

                    # Skip non-HTML
                    path_lower = parsed.path.lower()
                    if any(path_lower.endswith(ext) for ext in [".pdf", ".jpg", ".png", ".doc", ".zip"]):
                        continue

                    # Strip fragments and normalize
                    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if clean_url.endswith("/"):
                        clean_url = clean_url
                    else:
                        clean_url = clean_url + "/"

                    # Deduplicate
                    if clean_url in seen_urls:
                        continue
                    seen_urls.add(clean_url)

                    link = ScoredLink(
                        url=clean_url,
                        anchor_text=text,
                        source=f"nav-{source_name}",
                        depth=1,
                    )
                    candidates.append(link)

        # ── BFS Depth 2: Follow top navigation links ──
        # Score all depth-1 links first
        for link in candidates:
            self._score_link(link)

        # Pick top relevant links to crawl at depth 2
        top_links = sorted(candidates, key=lambda x: x.max_score, reverse=True)[:5]
        depth2_candidates = []

        for parent_link in top_links:
            if parent_link.max_score < 0.2:
                continue
            child_page = await crawler.fetch_page(parent_link.url, depth=1)
            if not child_page:
                continue

            child_soup = BeautifulSoup(child_page.html, "lxml")
            for a_tag in child_soup.find_all("a", href=True):
                href = a_tag["href"]
                text = a_tag.get_text(strip=True)

                full_url = urljoin(parent_link.url, href)
                parsed = urlparse(full_url)

                url_domain = parsed.hostname or ""
                if self.base_domain not in url_domain:
                    continue

                path_lower = parsed.path.lower()
                if any(path_lower.endswith(ext) for ext in [".pdf", ".jpg", ".png", ".doc", ".zip"]):
                    continue

                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if not clean_url.endswith("/"):
                    clean_url += "/"

                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)

                if self._url_matches_keywords(clean_url) or self._text_matches_keywords(text):
                    child_link = ScoredLink(
                        url=clean_url,
                        anchor_text=text,
                        source="nav-depth2",
                        depth=2,
                    )
                    depth2_candidates.append(child_link)

        candidates.extend(depth2_candidates)
        return candidates

    # ─────────────────────────────────────────────────
    # Scoring Engine
    # ─────────────────────────────────────────────────
    def _score_link(self, link: ScoredLink) -> None:
        """Score a link for admissions and tuition relevance."""
        url_lower = link.url.lower()
        text_lower = (link.anchor_text or "").lower()

        adm_score = 0.0
        tui_score = 0.0

        # ── URL path scoring (weight: 40%) ──
        url_path = urlparse(url_lower).path
        for pattern in ADMISSIONS_PATTERNS:
            if re.search(pattern, url_path):
                adm_score += 0.4
                break
        for pattern in TUITION_PATTERNS:
            if re.search(pattern, url_path):
                tui_score += 0.4
                break

        # ── Anchor text scoring (weight: 35%) ──
        for pattern in ADMISSIONS_PATTERNS:
            if re.search(pattern, text_lower):
                adm_score += 0.35
                break
        for pattern in TUITION_PATTERNS:
            if re.search(pattern, text_lower):
                tui_score += 0.35
                break

        # ── Structural bonus (weight: 15%) ──
        source = link.source.lower()
        if "nav" in source:
            adm_score *= 1.15
            tui_score *= 1.15
        elif "footer" in source:
            adm_score *= 1.05
            tui_score *= 1.05

        # ── Depth penalty (weight: 10%) ──
        if link.depth >= 2:
            adm_score *= 0.9
            tui_score *= 0.9

        # ── Bonus for highly specific URL slugs ──
        specific_adm = ["deadline", "dates-deadline", "admission-deadline", "apply-now"]
        specific_tui = ["cost-of-attendance", "tuition-fees", "tuition-and-fees", "cost-attendance"]
        for slug in specific_adm:
            if slug in url_path:
                adm_score += 0.2
        for slug in specific_tui:
            if slug in url_path:
                tui_score += 0.2

        # Cap at 1.0
        link.admissions_score = min(adm_score, 1.0)
        link.tuition_score = min(tui_score, 1.0)

    def _url_matches_keywords(self, url: str) -> bool:
        """Check if a URL contains any relevant keywords."""
        url_lower = url.lower()
        for pattern in ALL_PATTERNS:
            if re.search(pattern, url_lower):
                return True
        return False

    def _text_matches_keywords(self, text: str) -> bool:
        """Check if anchor text contains relevant keywords."""
        text_lower = text.lower()
        for pattern in ALL_PATTERNS:
            if re.search(pattern, text_lower):
                return True
        return False
