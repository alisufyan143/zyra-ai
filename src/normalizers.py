"""
Output normalization functions.
Applied after LLM extraction, before final Pydantic validation.
"""

import re
from datetime import datetime, date
from typing import Optional


# ── US State abbreviations ──
STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

# Reverse mapping for validation
VALID_ABBREVIATIONS = set(STATE_ABBREVIATIONS.values())


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """
    Normalize phone number to +1-XXX-XXX-XXXX format.
    Returns None if input is not a valid US phone number.
    """
    if not phone:
        return None

    # Strip everything except digits and +
    digits = re.sub(r"[^\d+]", "", phone)

    # Remove leading + and country code 1
    if digits.startswith("+1"):
        digits = digits[2:]
    elif digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]

    # Must be 10 digits for US
    if len(digits) != 10:
        return phone  # Return as-is if can't normalize

    return f"+1-{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Lowercase and validate email."""
    if not email:
        return None
    return email.lower().strip()


def normalize_state(state: Optional[str]) -> Optional[str]:
    """
    Normalize state to 2-letter abbreviation.
    Accepts: full name, abbreviation, mixed case.
    """
    if not state:
        return None

    state_clean = state.strip()

    # Already an abbreviation?
    if state_clean.upper() in VALID_ABBREVIATIONS:
        return state_clean.upper()

    # Full name lookup
    lookup = state_clean.lower()
    if lookup in STATE_ABBREVIATIONS:
        return STATE_ABBREVIATIONS[lookup]

    # Return as-is if unrecognized
    return state_clean


def normalize_country(country: Optional[str]) -> Optional[str]:
    """Normalize country name."""
    if not country:
        return None

    country_lower = country.strip().lower()
    us_variants = {"us", "usa", "u.s.", "u.s.a.", "united states of america", "united states"}

    if country_lower in us_variants:
        return "United States"

    return country.strip()


def normalize_postal_code(postal_code: Optional[str]) -> Optional[str]:
    """Normalize US postal codes to 5-digit or 5+4 format."""
    if not postal_code:
        return None

    # Strip whitespace
    code = postal_code.strip()

    # Extract digits and hyphens
    clean = re.sub(r"[^\d-]", "", code)

    # Validate formats: 12345 or 12345-6789
    if re.match(r"^\d{5}(-\d{4})?$", clean):
        return clean

    # Try extracting just 5 digits
    digits_only = re.sub(r"\D", "", code)
    if len(digits_only) == 5:
        return digits_only
    if len(digits_only) == 9:
        return f"{digits_only[:5]}-{digits_only[5:]}"

    return code  # Return as-is


def normalize_cost(cost) -> Optional[int]:
    """
    Normalize cost to integer (whole dollars).
    Strips $, commas, handles string inputs.
    Returns None for invalid values.
    """
    if cost is None:
        return None

    if isinstance(cost, int):
        if cost < 0:
            return None
        return cost

    if isinstance(cost, float):
        if cost < 0:
            return None
        return int(round(cost))

    if isinstance(cost, str):
        # Strip $ and commas
        clean = cost.strip().replace("$", "").replace(",", "").strip()
        try:
            value = int(float(clean))
            if value < 0:
                return None
            return value
        except (ValueError, TypeError):
            return None

    return None


def normalize_currency(currency: Optional[str], cost: Optional[int] = None) -> Optional[str]:
    """Default to USD if cost is present but currency is missing."""
    if currency:
        return currency.upper().strip()
    if cost is not None and cost > 0:
        return "USD"
    return None


def normalize_date(date_str: Optional[str]) -> Optional[str]:
    """
    Normalize date string to YYYY-MM-DD format.
    Handles various formats:
    - January 15, 2026
    - Jan 15
    - 1/15/2026
    - 01-15-2026
    - 2026-01-15 (already normalized)
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # Already in ISO format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    # Common date formats to try
    formats = [
        "%B %d, %Y",       # January 15, 2026
        "%b %d, %Y",       # Jan 15, 2026
        "%B %d %Y",        # January 15 2026
        "%b %d %Y",        # Jan 15 2026
        "%m/%d/%Y",        # 01/15/2026
        "%m-%d-%Y",        # 01-15-2026
        "%d %B %Y",        # 15 January 2026
        "%d %b %Y",        # 15 Jan 2026
        "%Y/%m/%d",        # 2026/01/15
    ]

    # Try formats with year
    for fmt in formats:
        try:
            parsed = datetime.strptime(date_str, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try without year — infer current/next academic year
    no_year_formats = [
        "%B %d",    # January 15
        "%b %d",    # Jan 15
        "%b. %d",   # Jan. 15
        "%m/%d",    # 01/15
    ]

    # Strip ordinal suffixes (1st, 2nd, 3rd, 15th)
    clean_date = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str)

    for fmt in no_year_formats:
        try:
            parsed = datetime.strptime(clean_date, fmt)
            # Infer year: if month is in the future, use current year;
            # otherwise use next year
            today = date.today()
            candidate = parsed.replace(year=today.year)
            if candidate.date() < today:
                candidate = parsed.replace(year=today.year + 1)
            return candidate.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Could not parse — return as-is
    return date_str


def normalize_fee_type(fee_type: Optional[str]) -> Optional[str]:
    """Clean up fee type string."""
    if not fee_type:
        return None
    # Collapse whitespace, strip, title case
    clean = re.sub(r"\s+", " ", fee_type.strip())
    return clean


def normalize_university_name(name: Optional[str]) -> Optional[str]:
    """Clean university name."""
    if not name:
        return None
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", name.strip())
    return clean
