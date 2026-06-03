"""
ETL Pipeline Orchestrator.
Ties together: validation -> discovery -> crawling -> extraction -> normalization -> quality -> output.
Full step-by-step logging at every stage.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from src.crawler import PlaywrightCrawler
from src.discovery import DiscoveryEngine
from src.extractor import GeminiExtractor, html_to_markdown
from src.llm_pool import LLMPool
from src.normalizers import (
    normalize_phone, normalize_email, normalize_state,
    normalize_country, normalize_postal_code, normalize_cost,
    normalize_currency, normalize_date, normalize_fee_type,
    normalize_university_name,
)
from src.quality import run_quality_checks
from src.schemas import (
    UniversityData, Overview, Location, Contact,
    TuitionItem, AdmissionDeadline, PageMetadata,
)
from src.validators import DomainInput, CrawledPage

logger = logging.getLogger(__name__)


class ETLPipeline:
    """
    Full ETL pipeline orchestrator.
    
    Flow:
    1. Validate & normalize input domain
    2. Discover relevant pages (sitemap + nav + scoring)
    3. Fetch pages with Playwright
    4. Extract data with Gemini LLM (with confidence + source tracking)
    5. Normalize outputs
    6. Quality checks + assemble + validate + save JSON
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._crawler: Optional[PlaywrightCrawler] = None
        self._extractor: Optional[GeminiExtractor] = None

    async def run(self, raw_domain: str, output_dir: str = "output") -> Optional[UniversityData]:
        """Run the full ETL pipeline for a single university."""
        run_start = datetime.now(timezone.utc)
        step_timings = {}

        logger.info("=" * 60)
        logger.info("PIPELINE START: %s", raw_domain)
        logger.info("=" * 60)

        # ── STEP 1: Validate & normalize input ──
        t0 = time.monotonic()
        logger.info("[Step 1/7] Validating and normalizing input domain...")
        try:
            validated_input = DomainInput(raw_url=raw_domain)
            base_url = validated_input.raw_url
            logger.info("  Input validated: %s -> %s", raw_domain, base_url)
        except Exception as e:
            logger.error("  Input validation FAILED: %s", e)
            logger.error("PIPELINE ABORTED for %s", raw_domain)
            return None
        step_timings["1_validation"] = time.monotonic() - t0

        # ── STEP 2: Discover pages ──
        t0 = time.monotonic()
        logger.info("[Step 2/7] Discovering relevant pages (sitemap + navigation)...")
        try:
            discovery = DiscoveryEngine(base_url.rstrip("/"))
            candidates = await discovery.discover(crawler=self._crawler)

            n_adm = len(candidates.admissions)
            n_tui = len(candidates.tuition)
            logger.info("  Discovery complete: %d admissions, %d tuition pages", n_adm, n_tui)

            if n_adm == 0:
                logger.warning("  WARNING: No admissions pages found!")
            if n_tui == 0:
                logger.warning("  WARNING: No tuition pages found!")

            top_urls = candidates.top_urls(limit=6)
            logger.info("  Selected %d pages for extraction:", len(top_urls))
            for url in top_urls:
                logger.info("    - %s", url)
        except Exception as e:
            logger.error("  Discovery FAILED: %s", e)
            logger.error("PIPELINE ABORTED for %s", raw_domain)
            return None
        step_timings["2_discovery"] = time.monotonic() - t0

        # ── STEP 3: Fetch pages with Playwright ──
        t0 = time.monotonic()
        logger.info("[Step 3/7] Fetching pages with Playwright browser...")
        all_pages: list[CrawledPage] = []
        try:
            pages = await self._crawler.fetch_pages_parallel(
                top_urls, max_concurrent=3, depth=0
            )
            all_pages = pages
            logger.info("  Fetched %d/%d pages successfully", len(pages), len(top_urls))
            for p in pages:
                logger.info("    [%d] %s (%d chars) - %s",
                            p.status_code, p.url, len(p.html),
                            (p.title[:60] if p.title else "no title"))

            if len(pages) == 0:
                logger.error("  No pages fetched! PIPELINE ABORTED.")
                return None
        except Exception as e:
            logger.error("  Crawling FAILED: %s", e)
            logger.error("PIPELINE ABORTED for %s", raw_domain)
            return None
        step_timings["3_crawling"] = time.monotonic() - t0

        # ── Classify fetched pages ──
        admissions_pages = []
        tuition_pages = []

        for page in pages:
            url_lower = page.url.lower()
            is_adm = any(kw in url_lower for kw in ["admiss", "deadline", "apply", "enroll"])
            is_tui = any(kw in url_lower for kw in ["tuition", "cost", "fee", "financial", "aid"])

            if is_adm:
                admissions_pages.append(page)
            if is_tui:
                tuition_pages.append(page)

        if not admissions_pages:
            logger.warning("  No admissions-specific pages, using all fetched pages as fallback")
            admissions_pages = pages
        if not tuition_pages:
            logger.warning("  No tuition-specific pages, using all fetched pages as fallback")
            tuition_pages = pages

        logger.info("  Classified: %d admissions pages, %d tuition pages",
                     len(admissions_pages), len(tuition_pages))

        # ── STEP 4: Extract data with Gemini LLM (with confidence) ──
        t0 = time.monotonic()
        logger.info("[Step 4/7] Extracting data with Gemini LLM (multi-key pool)...")
        extraction_sources = []
        extraction_confidence = []

        try:
            logger.info("  Extracting overview from %d pages...", len(pages))
            overview, ov_source, ov_conf = await self._extractor.extract_overview(pages)
            extraction_sources.append(ov_source)
            extraction_confidence.append(ov_conf)

            if overview:
                logger.info("    university_name: %s", overview.university_name)
                logger.info("    city: %s", overview.location.city if overview.location else "N/A")
                logger.info("    state: %s", overview.location.state if overview.location else "N/A")
                logger.info("    phone: %s", overview.contact.phone if overview.contact else "N/A")
                logger.info("    email: %s", overview.contact.email if overview.contact else "N/A")
                logger.info("    confidence: %s", ov_conf.overall_confidence.value)
            else:
                logger.warning("    Overview extraction returned None")

            logger.info("  Extracting tuition from %d pages...", len(tuition_pages))
            tuition, tui_source, tui_conf = await self._extractor.extract_tuition(tuition_pages)
            extraction_sources.append(tui_source)
            extraction_confidence.append(tui_conf)

            logger.info("    Extracted %d tuition items (confidence: %s)",
                        len(tuition), tui_conf.overall_confidence.value)
            for t in tuition:
                logger.info("      %s: $%s %s", t.fee_type, t.cost, t.currency)

            logger.info("  Extracting deadlines from %d pages...", len(admissions_pages))
            deadlines, ddl_source, ddl_conf = await self._extractor.extract_deadlines(admissions_pages)
            extraction_sources.append(ddl_source)
            extraction_confidence.append(ddl_conf)

            logger.info("    Extracted %d deadlines (confidence: %s)",
                        len(deadlines), ddl_conf.overall_confidence.value)
            for d in deadlines:
                logger.info("      %s: %s (%s)", d.deadline_type, d.deadline_date, d.notes)

        except Exception as e:
            logger.error("  LLM extraction FAILED: %s", e)
            overview = None
            tuition = []
            deadlines = []
        step_timings["4_extraction"] = time.monotonic() - t0

        # ── STEP 5: Normalize outputs ──
        t0 = time.monotonic()
        logger.info("[Step 5/7] Normalizing extracted data...")
        try:
            if overview:
                overview = Overview(
                    university_name=normalize_university_name(overview.university_name),
                    location=Location(
                        city=overview.location.city if overview.location else None,
                        state=normalize_state(overview.location.state if overview.location else None),
                        country=normalize_country(overview.location.country if overview.location else None),
                        postal_code=normalize_postal_code(overview.location.postal_code if overview.location else None),
                    ) if overview.location else None,
                    contact=Contact(
                        phone=normalize_phone(overview.contact.phone if overview.contact else None),
                        email=normalize_email(str(overview.contact.email) if overview.contact and overview.contact.email else None),
                    ) if overview.contact else None,
                )
                logger.info("  Overview normalized")

            normalized_tuition = []
            for t in tuition:
                cost = normalize_cost(t.cost)
                normalized_tuition.append(TuitionItem(
                    fee_type=normalize_fee_type(t.fee_type),
                    cost=cost,
                    currency=normalize_currency(t.currency, cost=cost),
                ))
            logger.info("  %d tuition items normalized", len(normalized_tuition))

            normalized_deadlines = []
            for d in deadlines:
                normalized_deadlines.append(AdmissionDeadline(
                    deadline_type=d.deadline_type,
                    deadline_date=normalize_date(d.deadline_date),
                    notes=d.notes,
                ))
            logger.info("  %d deadlines normalized", len(normalized_deadlines))

        except Exception as e:
            logger.error("  Normalization FAILED: %s", e)
            normalized_tuition = tuition
            normalized_deadlines = deadlines
        step_timings["5_normalization"] = time.monotonic() - t0

        # ── Build page metadata ──
        scraped_at = datetime.now(timezone.utc).isoformat()
        page_metadata = []
        for p in all_pages:
            page_metadata.append(PageMetadata(
                url=p.url,
                page_title=p.title,
                scraped_at=scraped_at,
                status_code=str(p.status_code),
            ))

        # ── STEP 6: Quality checks ──
        t0 = time.monotonic()
        logger.info("[Step 6/7] Running data quality checks...")
        pre_quality = UniversityData(
            overview=overview,
            tuition_breakdown=normalized_tuition,
            admission_deadlines=normalized_deadlines,
            page_metadata=page_metadata,
            extraction_sources=extraction_sources,
            extraction_confidence=extraction_confidence,
        )
        quality_report = run_quality_checks(pre_quality)
        step_timings["6_quality"] = time.monotonic() - t0

        # ── STEP 7: Assemble & validate final output ──
        t0 = time.monotonic()
        logger.info("[Step 7/7] Assembling and validating final output...")
        try:
            result = UniversityData(
                overview=overview,
                tuition_breakdown=normalized_tuition,
                admission_deadlines=normalized_deadlines,
                page_metadata=page_metadata,
                extraction_sources=extraction_sources,
                extraction_confidence=extraction_confidence,
                quality_report=quality_report,
            )

            # Final Pydantic validation roundtrip
            validated = UniversityData.model_validate(result.model_dump())
            logger.info("  Pydantic validation PASSED")

            # ── Save to JSON ──
            os.makedirs(output_dir, exist_ok=True)
            from src.utils import domain_to_filename
            filename = domain_to_filename(base_url) + ".json"
            output_path = os.path.join(output_dir, filename)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(validated.model_dump(), f, indent=2, default=str, ensure_ascii=False)

            logger.info("  Saved to: %s", output_path)

            # ── Summary ──
            run_time = (datetime.now(timezone.utc) - run_start).total_seconds()
            logger.info("-" * 60)
            logger.info("PIPELINE COMPLETE for %s", base_url)
            logger.info("  University:    %s", validated.overview.university_name if validated.overview else "N/A")
            logger.info("  Tuition:       %d items", len(validated.tuition_breakdown))
            logger.info("  Deadlines:     %d items", len(validated.admission_deadlines))
            logger.info("  Pages used:    %d", len(validated.page_metadata))
            logger.info("  Completeness:  %.0f%%", quality_report.completeness_score * 100)
            logger.info("  Quality issues: %d errors, %d warnings",
                        sum(1 for i in quality_report.issues if i.severity == "error"),
                        sum(1 for i in quality_report.issues if i.severity == "warning"))
            logger.info("  Output:        %s", output_path)
            logger.info("  Duration:      %.1fs", run_time)

            # Per-step timing breakdown
            logger.info("  Step timings:")
            for step_name, duration in step_timings.items():
                logger.info("    %s: %.1fs", step_name, duration)
            step_timings["7_assembly"] = time.monotonic() - t0

            logger.info("=" * 60)
            return validated

        except Exception as e:
            logger.error("  Final validation FAILED: %s", e)
            logger.error("PIPELINE FAILED for %s", raw_domain)
            return None

    async def run_batch(
        self, domains: list[str], output_dir: str = "output", log_file: str = None
    ) -> dict[str, Optional[UniversityData]]:
        """Run the pipeline for multiple universities."""
        from src.utils import setup_logging
        setup_logging(log_file=log_file, level=logging.INFO)

        logger.info("#" * 60)
        logger.info("#  University ETL Pipeline — Batch Run")
        logger.info("#  Universities: %d", len(domains))
        logger.info("#  Output dir:   %s", output_dir)
        logger.info("#  Log file:     %s", log_file or "console only")
        logger.info("#  Started at:   %s", datetime.now(timezone.utc).isoformat())
        logger.info("#" * 60)

        results = {}

        # Create shared LLM pool for key/model rotation across all universities
        pool = LLMPool()

        async with PlaywrightCrawler(headless=self.headless) as crawler:
            self._crawler = crawler
            self._extractor = GeminiExtractor(pool=pool)

            for i, domain in enumerate(domains, 1):
                logger.info("\n>>> Processing %d/%d: %s", i, len(domains), domain)
                try:
                    result = await self.run(domain, output_dir=output_dir)
                    results[domain] = result
                except Exception as e:
                    logger.error("UNHANDLED ERROR for %s: %s", domain, e)
                    results[domain] = None

        # ── Final batch summary ──
        logger.info("\n" + "=" * 60)
        logger.info("BATCH RUN COMPLETE")
        logger.info("=" * 60)
        succeeded = sum(1 for v in results.values() if v is not None)
        failed = len(results) - succeeded
        logger.info("  Succeeded: %d/%d", succeeded, len(results))
        logger.info("  Failed:    %d/%d", failed, len(results))
        for domain, result in results.items():
            status = "OK" if result else "FAILED"
            name = result.overview.university_name if result and result.overview else "N/A"
            tui = len(result.tuition_breakdown) if result else 0
            ddl = len(result.admission_deadlines) if result else 0
            score = f"{result.quality_report.completeness_score*100:.0f}%" if result and result.quality_report else "N/A"
            logger.info("    [%s] %s -> %s (T:%d D:%d Q:%s)",
                         status, domain, name, tui, ddl, score)

        # ── Log LLM pool stats ──
        logger.info("\n" + pool.get_stats())

        return results
