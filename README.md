# Zyra AI — University Data ETL Pipeline

Automated pipeline that extracts structured admissions, tuition, and deadline data from any US university website. Give it a domain, get clean JSON.

```
Input:  "bucknell.edu"
Output: { university_name, location, tuition_breakdown[], admission_deadlines[], ... }
```

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/alisufyan143/zyra-ai.git
cd zyra-ai
pip install -r requirements.txt
playwright install chromium
```

### 2. Set API Keys

Create a `.env` file in the project root:

```env
GEMINI_API_KEY_1=your-first-api-key
GEMINI_API_KEY_2=your-second-api-key
GEMINI_API_KEY_3=your-third-api-key
```

Get free keys at [ai.google.dev](https://ai.google.dev). Each key gives you 500 RPD on flash-lite. Three keys = 1500 requests/day.

### 3. Add Universities

Open `main.py` and add your domains:

```python
UNIVERSITIES = [
    "bucknell.edu",
    "stanford.edu",
    "purdue.edu",
]
```

### 4. Run

```bash
python main.py
```

Outputs land in `output/` as JSON files. Logs go to `temp-scripts/runs.log`.

---

## Architecture

```
main.py
  └─ ETLPipeline.run_batch()
       │
       ├─ Step 1: Validate & Normalize Input  (validators.py)
       │    "bucknell.edu" → "https://www.bucknell.edu/"
       │
       ├─ Step 2: Discover Pages  (discovery.py)
       │    Sitemap mining + navigation analysis → scored candidate URLs
       │
       ├─ Step 3: Fetch Pages  (crawler.py)
       │    Playwright browser → HTML content (with stealth fallback)
       │
       ├─ Step 4: Extract Data  (extractor.py + llm_pool.py)
       │    HTML → Markdown → Gemini LLM → structured Pydantic models
       │
       ├─ Step 5: Normalize  (normalizers.py)
       │    Phone, email, state, date, cost → clean canonical format
       │
       ├─ Step 6: Quality Checks  (quality.py)
       │    Missing fields, duplicates, date/cost sanity, completeness score
       │
       └─ Step 7: Assemble & Save  (schemas.py)
            Pydantic validation → JSON output
```

### File Map

| File | Purpose |
|---|---|
| `main.py` | Entry point — put your domains here |
| `src/pipeline.py` | Orchestrator — ties all steps together |
| `src/validators.py` | Input normalization, URL validation, CrawledPage validation |
| `src/discovery.py` | 2-layer page discovery (sitemap + navigation) |
| `src/crawler.py` | Playwright browser with stealth fallback + resource blocking |
| `src/extractor.py` | Gemini LLM extraction with confidence scoring |
| `src/llm_pool.py` | Multi-key, multi-model LLM rotation |
| `src/normalizers.py` | 10 pure functions for data cleaning |
| `src/quality.py` | Post-extraction data quality analysis |
| `src/schemas.py` | Pydantic output models |
| `src/utils.py` | Logging setup, filename helpers |

---

## Strategies

### 1. Page Discovery — How We Find the Right Pages

**Problem:** University websites have thousands of pages. We need only the 5-6 with tuition/deadline data.

**Strategy: 2-Layer Discovery with Keyword Scoring**

```
Layer 1: Sitemap Mining (aiohttp — fast, no browser)
├── Fetch /robots.txt → extract Sitemap: directives
├── Fetch /sitemap.xml, /sitemap_index.xml
├── Parse all <url> entries
└── Filter by keyword patterns: "tuition", "deadline", "admiss", "cost", etc.

Layer 2: Navigation Analysis (Playwright — real DOM)
├── Fetch homepage → parse <nav>, <header>, role="navigation"
├── Extract all <a> links with anchor text
├── Score each link:  URL keywords (40%) + anchor text (35%) + structural bonus (15%) + depth penalty (10%)
├── BFS depth-2: follow top 5 scored links → extract child links
└── Merge with Layer 1, boost scores for links found in both
```

**Example — Bucknell:**
```
Layer 1: Sitemap → 310 candidate URLs
Layer 2: Navigation → 95 candidates
Combined → 296 admissions, 208 tuition pages scored

Top picks:
  1.00  /admissions-aid/admissions-dates-deadlines/   ← URL has "admissions" + "deadline"
  1.00  /admissions-aid/tuition-fees-financial-aid/    ← URL has "tuition" + "fees" + "financial"
  0.86  /admissions-aid/apply-bucknell/                ← URL has "admissions" + "apply"
```

**Fallback:** If sitemap returns 0 results (common — Drexel, Stanford), Layer 2 navigation catches everything.

---

### 2. Crawling — How We Fetch Pages Fast

**Problem:** University sites are slow (images, fonts, analytics), some block bots.

**Strategy: Resource Blocking + Sticky Stealth**

```
Resource Blocking (at network level):
├── BLOCK: images (.jpg, .png, .webp, .svg, .gif, .ico)
├── BLOCK: fonts (.woff2, .woff, .ttf, .eot)
├── BLOCK: media (.mp4, .mp3, .avi)
├── BLOCK: stylesheets (.css)
├── BLOCK: analytics (google-analytics, googletagmanager, facebook, hotjar)
├── KEEP:  document (HTML)
├── KEEP:  script (JS — needed for dynamic content)
└── KEEP:  xhr/fetch (AJAX data loading)

Result: 12s page → 2-3s page (we only need text, not pixels)
```

**Sticky Stealth:**
```
Page 1 on spelman.edu: Standard fetch → bot detected → Stealth retry → success
  → Domain "www.spelman.edu" added to stealth list

Page 2 on spelman.edu: Stealth directly (no wasted standard attempt)
Page 3 on spelman.edu: Stealth directly
Page 4 on spelman.edu: Stealth directly
```

**Page Cache:** Same URL never fetched twice. Discovery fetches the homepage, pipeline reuses that cached result.

**Fallback Chain:**
```
Standard Chromium → fails?
  └─ Retry with playwright-stealth → fails?
       └─ Log error, mark URL as failed, skip (never retry again)
```

---

### 3. LLM Extraction — How We Get Structured Data

**Problem:** Free-tier Gemini has rate limits (20 RPD per model per key). A single university needs 3 LLM calls.

**Strategy: 3 Keys × 3 Models = 9 Fallback Slots**

```
Slot 0: Key-1 + gemini-3.1-flash-lite  (500 RPD) ← start here
Slot 1: Key-2 + gemini-3.1-flash-lite  (500 RPD)
Slot 2: Key-3 + gemini-3.1-flash-lite  (500 RPD)
Slot 3: Key-1 + gemini-3.5-flash       ( 20 RPD) ← fallback tier 2
Slot 4: Key-2 + gemini-3.5-flash       ( 20 RPD)
Slot 5: Key-3 + gemini-3.5-flash       ( 20 RPD)
Slot 6: Key-1 + gemini-2.5-flash       ( 20 RPD) ← fallback tier 3
Slot 7: Key-2 + gemini-2.5-flash       ( 20 RPD)
Slot 8: Key-3 + gemini-2.5-flash       ( 20 RPD)
```

**Round-robin example:**
```
University 1: overview → Slot 0, tuition → Slot 1, deadlines → Slot 2
University 2: overview → Slot 0, tuition → Slot 1, deadlines → Slot 2
...if Slot 0 hits rate limit...
University 5: overview → Slot 3 (fallback to 3.5-flash), tuition → Slot 4, ...
```

**Rate-limit detection:** Catches `429`, `503`, `"quota"`, `"resource_exhausted"`, `"too many requests"` in any error response.

**3 Focused Extraction Calls per University:**

| Call | Pages Fed | Output |
|---|---|---|
| `extract_overview` | Homepage + top 3 | Name, city, state, country, phone, email |
| `extract_tuition` | Tuition-classified pages | Fee type, cost (USD integer), currency |
| `extract_deadlines` | Admission-classified pages | Deadline type, date (YYYY-MM-DD), notes |

Each call returns **confidence scores** (high/medium/low per field) and **source URLs** (which pages were fed).

---

### 4. Normalization — How We Clean Data

**Problem:** Raw LLM output is messy — inconsistent phone formats, state names vs abbreviations, various date formats.

**Strategy: 10 Pure Functions, Each Handles One Field**

| Function | Input Example | Output |
|---|---|---|
| `normalize_phone` | `"(570) 577-3000"` | `"+1-570-577-3000"` |
| `normalize_email` | `"ADMISSIONS@Bucknell.EDU"` | `"admissions@bucknell.edu"` |
| `normalize_state` | `"Pennsylvania"` | `"PA"` |
| `normalize_country` | `"US"`, `"USA"`, `"United States of America"` | `"United States"` |
| `normalize_postal_code` | `"17837-2005"` | `"17837"` |
| `normalize_cost` | `"$54,890"`, `54890.0` | `54890` |
| `normalize_currency` | `"dollars"`, `"$"` | `"USD"` |
| `normalize_date` | `"November 1, 2025"`, `"11/01/2025"` | `"2025-11-01"` |
| `normalize_fee_type` | `"  tuition & FEES  "` | `"Tuition & Fees"` |
| `normalize_university_name` | `"THE BUCKNELL UNIVERSITY"` | `"Bucknell University"` |

---

### 5. Quality Checks — How We Catch Bad Data

**Problem:** LLM might return nulls, duplicates, impossible dates, or suspiciously low costs.

**Strategy: Post-Extraction Validation Report**

```
Quality Report for Bucknell University:
  Completeness: 90% (9/10 fields populated)
  Issues: 0 errors, 1 warnings
    [WARNING] missing_field: Optional field 'postal_code' is missing/null
  Duplicate tuition: 0
  Duplicate deadlines: 0
```

**Checks performed:**
- **Missing required fields** — `university_name` is flagged as ERROR if null
- **Missing optional fields** — city, state, phone, email flagged as WARNING
- **Duplicate tuition** — same fee_type + cost combination detected
- **Duplicate deadlines** — same type + date combination detected
- **Cost sanity** — flags costs below $10 or above $500,000
- **Date sanity** — flags dates before 2024 or after 2028, invalid formats

---

## Output Format

Each university produces a JSON file in `output/`:

```json
{
  "overview": {
    "university_name": "Bucknell University",
    "location": { "city": "Lewisburg", "state": "PA", "country": "United States" },
    "contact": { "phone": "+1-570-577-3000", "email": "admissions@bucknell.edu" }
  },
  "tuition_breakdown": [
    { "fee_type": "Tuition", "cost": 72600, "currency": "USD" },
    { "fee_type": "Housing (On Campus)", "cost": 11400, "currency": "USD" }
  ],
  "admission_deadlines": [
    { "deadline_type": "Early Decision", "deadline_date": "2025-11-01", "notes": "Binding" },
    { "deadline_type": "Regular Decision", "deadline_date": "2026-01-10", "notes": "..." }
  ],
  "extraction_sources": [
    { "extraction_type": "overview", "source_urls": ["https://..."], "model_used": "gemini-3.1-flash-lite" }
  ],
  "extraction_confidence": [
    { "extraction_type": "overview", "overall_confidence": "high", "field_scores": [...] }
  ],
  "quality_report": {
    "completeness_score": 0.9,
    "issues": [],
    "duplicate_tuition_count": 0,
    "duplicate_deadline_count": 0
  }
}
```

---

## Logging

Every run produces detailed step-by-step logs in `temp-scripts/runs.log`:

```
11:14:33 | PIPELINE START: berea.edu
11:14:33 | [Step 1/7] Validating and normalizing input domain...
11:14:33 |   Input validated: berea.edu -> https://www.berea.edu/
11:14:33 | [Step 2/7] Discovering relevant pages...
11:14:59 |   Discovery complete: 12 admissions, 8 tuition pages
11:15:02 | [Step 3/7] Fetching pages with Playwright...
11:15:10 |   Fetched 6/6 pages successfully
11:15:10 | [Step 4/7] Extracting data with Gemini LLM...
11:15:15 |   Overview: Berea College (confidence: high)
11:15:18 |   Tuition: 1 items (confidence: high)
11:15:20 | [Step 5/7] Normalizing...
11:15:20 | [Step 6/7] Quality checks: 60% complete, 0 errors, 5 warnings
11:15:20 | [Step 7/7] Saved to: output/www_berea_edu.json
11:15:20 | Duration: 48.3s
11:15:20 | Step timings:
11:15:20 |   2_discovery: 31.4s
11:15:20 |   3_crawling: 11.6s
11:15:20 |   4_extraction: 5.3s
```

Batch summaries at the end:

```
BATCH RUN COMPLETE
  Succeeded: 5/5
  [OK] stanford.edu  -> Stanford University    (T:1  D:0  Q:70%)
  [OK] spelman.edu   -> Spelman College        (T:18 D:0  Q:90%)
  [OK] purdue.edu    -> Purdue University      (T:0  D:0  Q:40%)
  [OK] berea.edu     -> Berea College          (T:1  D:0  Q:60%)
  [OK] drexel.edu    -> Drexel University      (T:0  D:0  Q:80%)

LLM Pool Usage Stats:
  Slot  Key    Model                 Reqs   Fails  Limited
  0     Key-1  gemini-3.1-flash-lite 4      0
  1     Key-2  gemini-3.1-flash-lite 3      0
  2     Key-3  gemini-3.1-flash-lite 3      0
  ...
  Total: 15 requests, 11 failures
```

---

## Tested Universities

| University | Tuition Items | Deadlines | Quality | Notes |
|---|---|---|---|---|
| Bucknell University | 17 | 5 | 90% | Full data — best result |
| Spelman College | 18 | 0 | 90% | Requires stealth for all pages |
| Frostburg State | 29 | 0 | 70% | In-state/out-of-state/regional breakdown |
| Stanford University | 1 | 0 | 70% | Tuition behind JS tabs |
| Drexel University | 0 | 0 | 80% | Good contact info, tuition on subdomain |
| Purdue University | 0 | 0 | 40% | Large site, tuition on different subdomain |
| MIT | 0 | 56 | 70% | 56 department-level grad deadlines extracted |
| Berea College | 1 | 0 | 60% | Correctly detected tuition-free model |
| Howard University | 1 | 0 | 70% | Parking cost extracted from financial services subdomain |

---

## Project Structure

```
zyra-ai/
├── main.py                    # Entry point — edit UNIVERSITIES list here
├── requirements.txt           # Python dependencies
├── .env                       # API keys (not committed)
├── src/
│   ├── pipeline.py            # 7-step orchestrator
│   ├── validators.py          # Input/URL/page validation
│   ├── discovery.py           # Sitemap + navigation page finder
│   ├── crawler.py             # Playwright browser with stealth + resource blocking
│   ├── extractor.py           # Gemini LLM extraction with confidence
│   ├── llm_pool.py            # Multi-key/model rotation pool
│   ├── normalizers.py         # Data cleaning functions
│   ├── quality.py             # Post-extraction quality analysis
│   ├── schemas.py             # Pydantic output models
│   └── utils.py               # Logging, helpers
├── output/                    # Generated JSON files
└── temp-scripts/              # Test scripts and run logs
    ├── runs.log               # Persistent execution log
    ├── test_step1.py - test_step8.py  # Per-step validation
    ├── test_llm_pool.py       # LLM pool unit tests (mock-based)
    ├── test_5_universities.py # Batch test for 5 universities
    └── validate_all_steps.py  # Unified test runner (272 checks)
```