"""
LLM-based data extraction using the multi-key, multi-model LLM pool.
Extracts structured university data from cleaned page markdown.
Includes per-field confidence scoring and source URL attribution.
"""

import logging
import re
from functools import lru_cache
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify
from pydantic import BaseModel

from src.llm_pool import LLMPool
from src.schemas import (
    AdmissionDeadline,
    ConfidenceLevel,
    Contact,
    ExtractionConfidence,
    ExtractionSource,
    FieldConfidence,
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


# ── Wrapper models with confidence ──

class OverviewWithConfidence(BaseModel):
    """Overview extraction with per-field confidence."""
    university_name: Optional[str] = None
    university_name_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM

    city: Optional[str] = None
    city_confidence: ConfidenceLevel = ConfidenceLevel.LOW

    state: Optional[str] = None
    state_confidence: ConfidenceLevel = ConfidenceLevel.LOW

    country: Optional[str] = None
    country_confidence: ConfidenceLevel = ConfidenceLevel.LOW

    postal_code: Optional[str] = None
    postal_code_confidence: ConfidenceLevel = ConfidenceLevel.LOW

    phone: Optional[str] = None
    phone_confidence: ConfidenceLevel = ConfidenceLevel.LOW

    email: Optional[str] = None
    email_confidence: ConfidenceLevel = ConfidenceLevel.LOW


class TuitionItemWithConfidence(BaseModel):
    fee_type: Optional[str] = None
    cost: Optional[int] = None
    currency: Optional[str] = None
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class TuitionListWithConfidence(BaseModel):
    items: list[TuitionItemWithConfidence]
    overall_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class DeadlineWithConfidence(BaseModel):
    deadline_type: Optional[str] = None
    deadline_date: Optional[str] = None
    notes: Optional[str] = None
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


class DeadlineListWithConfidence(BaseModel):
    items: list[DeadlineWithConfidence]
    overall_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


@lru_cache(maxsize=32)
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


class ExtractionResult:
    """Container for extraction output + metadata."""

    def __init__(self):
        self.overview: Optional[Overview] = None
        self.tuition: list[TuitionItem] = []
        self.deadlines: list[AdmissionDeadline] = []
        self.sources: list[ExtractionSource] = []
        self.confidence: list[ExtractionConfidence] = []


class GeminiExtractor:
    """
    Extracts structured university data from page content.
    Uses the LLMPool for automatic key rotation and model fallback.
    3 focused extraction calls with confidence scoring and source tracking.
    """

    def __init__(self, pool: LLMPool = None):
        self.pool = pool or LLMPool()

    async def extract_overview(self, pages: list) -> tuple[Optional[Overview], ExtractionSource, ExtractionConfidence]:
        """
        Call 1: Extract university overview (name, location, contact).
        Returns (Overview, source_info, confidence_info).
        """
        source_urls = [p.url for p in pages[:3]]
        source = ExtractionSource(
            extraction_type="overview",
            source_urls=source_urls,
        )
        empty_confidence = ExtractionConfidence(
            extraction_type="overview",
            overall_confidence=ConfidenceLevel.LOW,
        )

        if not pages:
            return None, source, empty_confidence

        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"
        combined_md = combined_md[:MAX_CONTENT_CHARS]

        prompt = f"""You are a university data extraction specialist.

Extract the university overview information from the following web page content.
For EACH field, also rate your confidence as "high", "medium", or "low":
- "high" = clearly stated in the text (e.g., university name in a heading)
- "medium" = reasonably inferred from context
- "low" = guessed or partially visible

STRICT RULES:
- Return null for any field you cannot find with reasonable confidence.
- Do NOT fabricate or guess values.
- For email: only return a valid email address (user@domain.com).
- For phone: return the phone number exactly as found on the page.
- For state: return the 2-letter US state abbreviation if it's a US university.
- For country: default to "United States" if it appears to be a US university.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=OverviewWithConfidence,
            )
            if not result:
                return None, source, empty_confidence

            # Record which model served this call
            source.model_used = self._get_last_model()

            # Build confidence report
            field_scores = []
            for field_name in ["university_name", "city", "state", "country", "postal_code", "phone", "email"]:
                conf = getattr(result, f"{field_name}_confidence", ConfidenceLevel.LOW)
                value = getattr(result, field_name, None)
                if value is not None:
                    field_scores.append(FieldConfidence(
                        field_name=field_name,
                        confidence=conf,
                        reason=f"Value: {value}"
                    ))

            # Determine overall confidence
            if field_scores:
                high_count = sum(1 for f in field_scores if f.confidence == ConfidenceLevel.HIGH)
                overall = ConfidenceLevel.HIGH if high_count >= 3 else (
                    ConfidenceLevel.MEDIUM if high_count >= 1 else ConfidenceLevel.LOW
                )
            else:
                overall = ConfidenceLevel.LOW

            confidence = ExtractionConfidence(
                extraction_type="overview",
                overall_confidence=overall,
                field_scores=field_scores,
            )

            # Convert to standard Overview
            from pydantic import ValidationError
            try:
                email_val = result.email if result.email and "@" in str(result.email) else None
                overview = Overview(
                    university_name=result.university_name,
                    location=Location(
                        city=result.city,
                        state=result.state,
                        country=result.country,
                        postal_code=result.postal_code,
                    ),
                    contact=Contact(
                        phone=result.phone,
                        email=email_val,
                    ),
                )
            except ValidationError as e:
                logger.warning("Overview validation issue: %s", e)
                overview = Overview(
                    university_name=result.university_name,
                    location=Location(city=result.city, state=result.state, country=result.country),
                    contact=Contact(phone=result.phone),
                )

            logger.info("Overview extracted: %s (confidence: %s)", result.university_name, overall.value)
            return overview, source, confidence

        except Exception as e:
            logger.error("Overview extraction failed: %s", e)
            return None, source, empty_confidence

    async def extract_tuition(self, pages: list) -> tuple[list[TuitionItem], ExtractionSource, ExtractionConfidence]:
        """
        Call 2: Extract tuition breakdown items from cost/fee pages.
        Returns (list[TuitionItem], source_info, confidence_info).
        """
        source_urls = [p.url for p in pages[:3]]
        source = ExtractionSource(extraction_type="tuition", source_urls=source_urls)
        empty_confidence = ExtractionConfidence(
            extraction_type="tuition",
            overall_confidence=ConfidenceLevel.LOW,
        )

        if not pages:
            return [], source, empty_confidence

        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"
        combined_md = combined_md[:MAX_CONTENT_CHARS]

        prompt = f"""You are a university data extraction specialist.

Extract ALL tuition and fee items from the following web page content.
For each item, rate your confidence as "high", "medium", or "low":
- "high" = dollar amount clearly shown in a table or list
- "medium" = amount found in paragraph text
- "low" = estimated or partially visible

Also rate your overall_confidence for the entire tuition extraction.

STRICT RULES:
- Extract every individual fee row (tuition, room, board, fees, etc.).
- For 'cost': return as a whole integer number of US dollars (e.g. 54890).
- For 'currency': always use "USD" for US dollar amounts.
- For 'fee_type': use descriptive names like "Tuition", "Room and Board", etc.
- Do NOT fabricate costs.
- Return an empty list if no tuition data is found.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=TuitionListWithConfidence,
            )
            if not result:
                return [], source, empty_confidence

            source.model_used = self._get_last_model()

            field_scores = []
            for i, item in enumerate(result.items):
                field_scores.append(FieldConfidence(
                    field_name=f"tuition[{i}].{item.fee_type}",
                    confidence=item.confidence,
                    reason=f"${item.cost}" if item.cost else "no cost",
                ))

            confidence = ExtractionConfidence(
                extraction_type="tuition",
                overall_confidence=result.overall_confidence,
                field_scores=field_scores,
            )

            # Convert to standard TuitionItem list
            items = [
                TuitionItem(fee_type=t.fee_type, cost=t.cost, currency=t.currency)
                for t in result.items
            ]

            logger.info("Tuition extracted: %d items (confidence: %s)",
                        len(items), result.overall_confidence.value)
            return items, source, confidence

        except Exception as e:
            logger.error("Tuition extraction failed: %s", e)
            return [], source, empty_confidence

    async def extract_deadlines(self, pages: list) -> tuple[list[AdmissionDeadline], ExtractionSource, ExtractionConfidence]:
        """
        Call 3: Extract admission deadlines from admissions pages.
        Returns (list[AdmissionDeadline], source_info, confidence_info).
        """
        source_urls = [p.url for p in pages[:3]]
        source = ExtractionSource(extraction_type="deadlines", source_urls=source_urls)
        empty_confidence = ExtractionConfidence(
            extraction_type="deadlines",
            overall_confidence=ConfidenceLevel.LOW,
        )

        if not pages:
            return [], source, empty_confidence

        combined_md = ""
        for page in pages[:3]:
            md = html_to_markdown(page.html)
            combined_md += f"\n\n## Page: {page.url}\n{md}"
        combined_md = combined_md[:MAX_CONTENT_CHARS]

        prompt = f"""You are a university data extraction specialist.

Extract ALL admission deadlines from the following web page content.
For each item, rate your confidence as "high", "medium", or "low":
- "high" = date clearly stated with explicit deadline label
- "medium" = date found but context is somewhat ambiguous
- "low" = date inferred or partially visible

Also rate your overall_confidence for the entire deadline extraction.

STRICT RULES:
- For 'deadline_type': use ONLY: "Early Decision", "Regular Decision", or "Transfer Admission".
  If it doesn't match, set deadline_type to null and explain in 'notes'.
- For 'deadline_date': normalize to YYYY-MM-DD format.
- Do NOT fabricate deadlines.
- Return an empty list if no deadlines are found.

PAGE CONTENT:
{combined_md}
"""
        try:
            result = await self.pool.call(
                messages=[{"role": "user", "content": prompt}],
                response_model=DeadlineListWithConfidence,
            )
            if not result:
                return [], source, empty_confidence

            source.model_used = self._get_last_model()

            field_scores = []
            for i, item in enumerate(result.items):
                field_scores.append(FieldConfidence(
                    field_name=f"deadline[{i}].{item.deadline_type or 'other'}",
                    confidence=item.confidence,
                    reason=f"{item.deadline_date} ({item.notes})" if item.deadline_date else "no date",
                ))

            confidence = ExtractionConfidence(
                extraction_type="deadlines",
                overall_confidence=result.overall_confidence,
                field_scores=field_scores,
            )

            # Convert to standard AdmissionDeadline list
            from src.schemas import DeadlineType
            items = []
            for d in result.items:
                dtype = None
                if d.deadline_type:
                    try:
                        dtype = DeadlineType(d.deadline_type)
                    except ValueError:
                        pass
                items.append(AdmissionDeadline(
                    deadline_type=dtype,
                    deadline_date=d.deadline_date,
                    notes=d.notes,
                ))

            logger.info("Deadlines extracted: %d items (confidence: %s)",
                        len(items), result.overall_confidence.value)
            return items, source, confidence

        except Exception as e:
            logger.error("Deadline extraction failed: %s", e)
            return [], source, empty_confidence

    def _get_last_model(self) -> Optional[str]:
        """Get the model name from the last successful pool call."""
        for slot in self.pool._slots:
            if slot.requests > 0:
                return slot.model
        return None
