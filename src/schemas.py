"""
Pydantic schema models for university data extraction.
Defines the exact output structure for the ETL pipeline.
"""

from enum import Enum
from typing import List, Optional

# pyrefly: ignore [missing-import]
from pydantic import BaseModel, EmailStr


class Location(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None


class Contact(BaseModel):
    phone: Optional[str] = None
    email: Optional[EmailStr] = None


class Overview(BaseModel):
    university_name: Optional[str] = None
    location: Optional[Location] = None
    contact: Optional[Contact] = None


class TuitionItem(BaseModel):
    fee_type: Optional[str] = None
    cost: Optional[int] = None
    currency: Optional[str] = None


class DeadlineType(str, Enum):
    EARLY_DECISION = "Early Decision"
    REGULAR_DECISION = "Regular Decision"
    TRANSFER_ADMISSION = "Transfer Admission"


class AdmissionDeadline(BaseModel):
    deadline_type: Optional[DeadlineType] = None
    deadline_date: Optional[str] = None
    notes: Optional[str] = None


class PageMetadata(BaseModel):
    url: Optional[str] = None
    page_title: Optional[str] = None
    scraped_at: Optional[str] = None
    status_code: Optional[str] = None


# ── Source Attribution ──

class ExtractionSource(BaseModel):
    """Tracks which pages fed each extraction call and the model used."""
    extraction_type: str              # "overview", "tuition", "deadlines"
    source_urls: List[str] = []       # Pages fed to this extraction call
    model_used: Optional[str] = None  # e.g. "gemini-3.1-flash-lite"
    slot_info: Optional[str] = None   # e.g. "Slot 0 (Key-1)"


# ── Confidence Scoring ──

class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FieldConfidence(BaseModel):
    """Confidence rating for a specific extracted field."""
    field_name: str
    confidence: ConfidenceLevel
    reason: Optional[str] = None


class ExtractionConfidence(BaseModel):
    """Per-extraction-call confidence ratings."""
    extraction_type: str                    # "overview", "tuition", "deadlines"
    overall_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    field_scores: List[FieldConfidence] = []


# ── Data Quality Report ──

class QualityIssue(BaseModel):
    """A single data quality finding."""
    severity: str       # "error", "warning", "info"
    category: str       # "missing_field", "duplicate", "date_sanity", "cost_sanity"
    message: str
    field: Optional[str] = None


class DataQualityReport(BaseModel):
    """Post-extraction data quality analysis."""
    total_fields_expected: int = 0
    total_fields_populated: int = 0
    completeness_score: float = 0.0        # 0.0 - 1.0
    issues: List[QualityIssue] = []
    duplicate_tuition_count: int = 0
    duplicate_deadline_count: int = 0


# ── Main Output ──

class UniversityData(BaseModel):
    overview: Optional[Overview] = None
    tuition_breakdown: List[TuitionItem] = []
    admission_deadlines: List[AdmissionDeadline] = []
    page_metadata: List[PageMetadata] = []
    extraction_sources: List[ExtractionSource] = []
    extraction_confidence: List[ExtractionConfidence] = []
    quality_report: Optional[DataQualityReport] = None
