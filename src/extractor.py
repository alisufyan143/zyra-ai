"""
LLM-based data extraction using the multi-key, multi-model LLM pool.
Extracts structured university data from cleaned page markdown.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify
from pydantic import BaseModel

from src.llm_pool import LLMPool
from src.schemas import (
    AdmissionDeadline,
    Contact,
    Location,
    Overview,
    TuitionItem,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ── Tags to strip before LLM — noise only ──
STRIP_TAGS = [
    "script", "style", "noscript", "iframe",
    "nav", "footer", "header",
    "form", "button", "input", "select", "textarea",
    "aside", "svg", "img", "video", "audio",
]

# ── Max characters sent to LLM (~10k tokens) ──
MAX_CONTENT_CHARS = 40_000


def html_to_markdown(html: str) -> str:
    """
    Convert HTML to clean markdown for LLM consumption.
    Strips noise, keeps tables and content.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noise tags
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()

    # Remove cookie banners and popups by common class names
    noise_classes = [
        "cookie", "banner", "popup", "modal", "overlay",
        "notification", "alert", "breadcrumb", "pagination",
        "social", "share", "print", "skip",
    ]
    for cls in noise_classes:
        for el in soup.find_all(class_=lambda c: c and cls in " ".join(c).lower()):
            el.decompose()

    # Convert to markdown
    md = markdownify(str(soup), heading_style="ATX", strip=["a"])

    # Collapse excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = md.strip()

    # Truncate to max length
    if len(md) > MAX_CONTENT_CHARS:
        md = md[:MAX_CONTENT_CHARS] + "\n\n[Content truncated...]"

    return md


class GeminiExtractor:
    """
    Extracts structured university data from page content.
    Uses the LLMPool for automatic key rotation and model fallback.
    3 focused extraction calls for higher accuracy.
    """

    def __init__(self, pool: LLMPool = None):
        """
        Args:
            pool: Shared LLMPool instance. Creates one if not provided.
        """
        self.pool = pool or LLMPool()

    async def extract_overview(self, pages: list) -> Optional[Overview]:
        """
        Call 1: Extract university overview (name, location, contact).
        Feeds multiple pages for best signal.
        """
        if not pages:
            return None

        # Combine content from provided pages
        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"

        combined_md = combined_md[:MAX_CONTENT_CHARS]

        prompt = f"""You are a university data extraction specialist.

Extract the university overview information from the following web page content.

STRICT RULES:
- Return null for any field you cannot find with reasonable confidence.
- Do NOT fabricate or guess values.
- For email: only return a valid email address format (user@domain.com). If the email is obfuscated or unclear, return null.
- For phone: return the phone number exactly as found on the page (e.g. "(570) 577-1101").
- For state: return the 2-letter US state abbreviation if it's a US university.
- For country: default to "United States" if it appears to be a US university.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=Overview,
            )
            if result:
                logger.info("Overview extracted: %s", result.university_name)
            return result
        except Exception as e:
            logger.error("Overview extraction failed: %s", e)
            return None

    async def extract_tuition(self, pages: list) -> list[TuitionItem]:
        """
        Call 2: Extract tuition breakdown items from cost/fee pages.
        """
        if not pages:
            return []

        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"

        combined_md = combined_md[:MAX_CONTENT_CHARS]

        class TuitionList(BaseModel):
            items: list[TuitionItem]

        prompt = f"""You are a university data extraction specialist.

Extract ALL tuition and fee items from the following web page content.

STRICT RULES:
- Extract every individual fee row (tuition, room, board, fees, etc.).
- For 'cost': return as a whole integer number of US dollars (e.g. 54890). Strip any $ signs or commas.
- For 'currency': always use "USD" for US dollar amounts.
- For 'fee_type': use descriptive names like "Tuition", "Room and Board", "Student Fees", "In-State Tuition", etc.
- If you see per-semester costs, note it in fee_type (e.g. "Tuition (per semester)").
- Do NOT fabricate costs. If no clear dollar amount is visible, omit that item.
- Return an empty list if no tuition data is found.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=TuitionList,
            )
            if result:
                logger.info("Tuition extracted: %d items", len(result.items))
                return result.items
            return []
        except Exception as e:
            logger.error("Tuition extraction failed: %s", e)
            return []

    async def extract_deadlines(self, pages: list) -> list[AdmissionDeadline]:
        """
        Call 3: Extract admission deadlines from admissions pages.
        """
        if not pages:
            return []

        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"

        combined_md = combined_md[:MAX_CONTENT_CHARS]

        class DeadlineList(BaseModel):
            items: list[AdmissionDeadline]

        prompt = f"""You are a university data extraction specialist.

Extract ALL admission deadlines from the following web page content.

STRICT RULES:
- For 'deadline_type': use ONLY one of these exact values:
    "Early Decision" — binding early application
    "Regular Decision" — standard application deadline
    "Transfer Admission" — for transfer students
  If the deadline type doesn't match any of these, set deadline_type to null and explain in 'notes'.
- For 'deadline_date': normalize to YYYY-MM-DD format. If no year given, assume the next upcoming academic year (2025-2026).
- For 'notes': include any important additional info (e.g. "Binding", "Rolling admission", "Priority deadline").
- Do NOT fabricate deadlines.
- Return an empty list if no deadlines are found.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=DeadlineList,
            )
            if result:
                logger.info("Deadlines extracted: %d items", len(result.items))
                return result.items
            return []
        except Exception as e:
            logger.error("Deadline extraction failed: %s", e)
            return []
