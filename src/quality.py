"""
Data Quality Checks — Post-extraction validation and reporting.

Runs after extraction + normalization, before final JSON save.
Detects: missing required fields, duplicates, date/cost sanity issues.
Produces a DataQualityReport with completeness score.
"""

import logging
import re
from datetime import datetime, date
from typing import Optional

from src.schemas import (
    UniversityData,
    DataQualityReport,
    QualityIssue,
)

logger = logging.getLogger(__name__)

# ── Cost sanity bounds ──
MIN_REASONABLE_COST = 10           # $10 minimum
MAX_REASONABLE_COST = 500_000      # $500k maximum (some MBA programs)

# ── Date sanity bounds ──
MIN_REASONABLE_YEAR = 2024
MAX_REASONABLE_YEAR = 2028


def run_quality_checks(data: UniversityData) -> DataQualityReport:
    """
    Run all data quality checks on the final extracted data.
    
    Returns a DataQualityReport attached to the UniversityData.
    """
    issues: list[QualityIssue] = []

    # ── Track field population ──
    expected_fields = 0
    populated_fields = 0

    def check_field(field_name: str, value, required: bool = False):
        nonlocal expected_fields, populated_fields
        expected_fields += 1
        if value is not None and value != "" and value != []:
            populated_fields += 1
        else:
            severity = "error" if required else "warning"
            issues.append(QualityIssue(
                severity=severity,
                category="missing_field",
                message=f"{'Required' if required else 'Optional'} field '{field_name}' is missing/null",
                field=field_name,
            ))

    # ── 1. Overview field checks ──
    logger.info("  Quality: checking overview fields...")
    if data.overview:
        check_field("university_name", data.overview.university_name, required=True)

        if data.overview.location:
            check_field("city",        data.overview.location.city,        required=False)
            check_field("state",       data.overview.location.state,       required=False)
            check_field("country",     data.overview.location.country,     required=False)
            check_field("postal_code", data.overview.location.postal_code, required=False)
        else:
            for f in ["city", "state", "country", "postal_code"]:
                check_field(f, None, required=False)

        if data.overview.contact:
            check_field("phone", data.overview.contact.phone, required=False)
            check_field("email", data.overview.contact.email, required=False)
        else:
            for f in ["phone", "email"]:
                check_field(f, None, required=False)
    else:
        issues.append(QualityIssue(
            severity="error",
            category="missing_field",
            message="Entire overview section is missing",
            field="overview",
        ))
        for f in ["university_name", "city", "state", "country", "postal_code", "phone", "email"]:
            expected_fields += 1

    # ── 2. Tuition checks ──
    logger.info("  Quality: checking tuition data...")
    check_field("tuition_breakdown", data.tuition_breakdown if data.tuition_breakdown else None, required=False)

    # Duplicate detection: same fee_type + cost
    seen_tuition = set()
    dup_tuition = 0
    for t in data.tuition_breakdown:
        key = (t.fee_type, t.cost)
        if key in seen_tuition:
            dup_tuition += 1
            issues.append(QualityIssue(
                severity="warning",
                category="duplicate",
                message=f"Duplicate tuition entry: '{t.fee_type}' = ${t.cost}",
                field="tuition_breakdown",
            ))
        seen_tuition.add(key)

        # Cost sanity
        if t.cost is not None:
            if t.cost < MIN_REASONABLE_COST:
                issues.append(QualityIssue(
                    severity="warning",
                    category="cost_sanity",
                    message=f"Suspiciously low cost: '{t.fee_type}' = ${t.cost} (< ${MIN_REASONABLE_COST})",
                    field="tuition_breakdown",
                ))
            if t.cost > MAX_REASONABLE_COST:
                issues.append(QualityIssue(
                    severity="warning",
                    category="cost_sanity",
                    message=f"Suspiciously high cost: '{t.fee_type}' = ${t.cost} (> ${MAX_REASONABLE_COST:,})",
                    field="tuition_breakdown",
                ))

    # ── 3. Deadline checks ──
    logger.info("  Quality: checking deadline data...")
    check_field("admission_deadlines", data.admission_deadlines if data.admission_deadlines else None, required=False)

    # Duplicate detection: same type + date
    seen_deadlines = set()
    dup_deadlines = 0
    for d in data.admission_deadlines:
        dtype = d.deadline_type.value if d.deadline_type else "None"
        key = (dtype, d.deadline_date)
        if key in seen_deadlines:
            dup_deadlines += 1
            issues.append(QualityIssue(
                severity="warning",
                category="duplicate",
                message=f"Duplicate deadline: {dtype} on {d.deadline_date}",
                field="admission_deadlines",
            ))
        seen_deadlines.add(key)

        # Date sanity
        if d.deadline_date and re.match(r"^\d{4}-\d{2}-\d{2}$", d.deadline_date):
            try:
                parsed = datetime.strptime(d.deadline_date, "%Y-%m-%d").date()
                year = parsed.year
                if year < MIN_REASONABLE_YEAR:
                    issues.append(QualityIssue(
                        severity="warning",
                        category="date_sanity",
                        message=f"Deadline date in the past: {dtype} = {d.deadline_date} (year {year} < {MIN_REASONABLE_YEAR})",
                        field="admission_deadlines",
                    ))
                if year > MAX_REASONABLE_YEAR:
                    issues.append(QualityIssue(
                        severity="warning",
                        category="date_sanity",
                        message=f"Deadline date too far in future: {dtype} = {d.deadline_date} (year {year} > {MAX_REASONABLE_YEAR})",
                        field="admission_deadlines",
                    ))
                # Check for impossible dates (Feb 30, etc.) — strptime already handles this
            except ValueError:
                issues.append(QualityIssue(
                    severity="error",
                    category="date_sanity",
                    message=f"Invalid date format for {dtype}: {d.deadline_date}",
                    field="admission_deadlines",
                ))
        elif d.deadline_date:
            issues.append(QualityIssue(
                severity="warning",
                category="date_sanity",
                message=f"Date not in YYYY-MM-DD format for {dtype}: {d.deadline_date}",
                field="admission_deadlines",
            ))

    # ── 4. Page metadata checks ──
    check_field("page_metadata", data.page_metadata if data.page_metadata else None, required=False)

    # ── Compute completeness score ──
    completeness = populated_fields / expected_fields if expected_fields > 0 else 0.0

    report = DataQualityReport(
        total_fields_expected=expected_fields,
        total_fields_populated=populated_fields,
        completeness_score=round(completeness, 3),
        issues=issues,
        duplicate_tuition_count=dup_tuition,
        duplicate_deadline_count=dup_deadlines,
    )

    # ── Log the report ──
    errors = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")
    logger.info("  Quality Report:")
    logger.info("    Completeness: %.0f%% (%d/%d fields)",
                completeness * 100, populated_fields, expected_fields)
    logger.info("    Issues: %d errors, %d warnings", errors, warnings)
    logger.info("    Duplicate tuition: %d, Duplicate deadlines: %d",
                dup_tuition, dup_deadlines)

    for issue in issues:
        if issue.severity == "error":
            logger.error("    [%s] %s: %s", issue.severity.upper(), issue.category, issue.message)
        else:
            logger.warning("    [%s] %s: %s", issue.severity.upper(), issue.category, issue.message)

    return report
