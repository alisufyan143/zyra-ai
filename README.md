# University ETL Data Pipeline

An automated, intelligent ETL (Extract, Transform, Load) pipeline designed to discover, crawl, and extract structured admissions and tuition data from university websites using Playwright and Google's Gemini LLM.

## 🌟 Key Features

- **Intelligent Discovery Engine:** Automatically mines sitemaps, analyzes navigation menus, and uses keyword scoring to find relevant Admissions and Tuition/Cost pages. Hardcoded URLs are not required.
- **Robust Crawler:** Built on Playwright with stealth-mode fallbacks to handle JavaScript-heavy sites and bypass basic bot protections.
- **Multi-Model LLM Extraction:** Uses Google's Gemini Flash models via `instructor` to extract structured JSON data directly from raw HTML/Markdown.
- **Resilient LLM Pool:** Implements a multi-key, multi-model connection pool with automatic rotation, rate-limit handling (HTTP 429), and fallback (e.g., `gemini-3.1-flash-lite` → `gemini-3.5-flash` → `gemini-2.5-flash`).
- **Data Normalization & Validation:** Strict Pydantic schemas guarantee data quality. All extracted data (phones, emails, costs, dates, states) is rigorously cleaned and normalized.
- **Batch Processing:** Run multiple university domains in sequence with comprehensive step-by-step logging.

## 🏗️ Architecture

1. **Input Validation:** Normalizes domains (e.g., `bucknell.edu` → `https://www.bucknell.edu/`).
2. **Discovery (`src/discovery.py`):** 3-layer approach (Sitemap → BFS Navigation → Keyword Scoring) to identify top target pages.
3. **Crawling (`src/crawler.py`):** Asynchronous, parallel fetching using Playwright.
4. **Extraction (`src/extractor.py`):** Converts HTML to clean Markdown and uses Gemini to extract `Overview`, `Tuition`, and `Deadlines`.
5. **Normalization (`src/normalizers.py`):** Cleans outputs into standard formats (e.g., ISO dates, integer costs, standard phone formats).
6. **Output:** Saves validated Pydantic models as structured JSON files.

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- Conda (recommended for environment management)

### Installation

1. **Clone the repository:**
   ```powershell
   git clone <repository-url>
   cd zyra-ai
   ```

2. **Create and activate a virtual environment:**
   ```powershell
   conda create -n trail python=3.10
   conda activate trail
   ```

3. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```

4. **Configure API Keys:**
   Copy the example environment file and add your Gemini API keys. The system supports multiple keys for rate-limit rotation.
   ```powershell
   cp .env.example .env
   ```
   Edit `.env`:
   ```env
   GEMINI_API_KEY_1=your_first_api_key
   GEMINI_API_KEY_2=your_second_api_key
   GEMINI_API_KEY_3=your_third_api_key
   ```

## 💻 Usage

### Running the Pipeline

Edit the `UNIVERSITIES` list inside `main.py` to include the target domains you want to process.

```python
# main.py
UNIVERSITIES = [
    "bucknell.edu",
    "udc.edu",
    "salisbury.edu",
]
```

Run the orchestrator:

```powershell
python main.py
```

### Outputs

- **JSON Data:** Extracted data is saved to the `./output/` directory (e.g., `www_bucknell_edu.json`).
- **Logs:** Detailed execution logs are appended to `temp-scripts/runs.log`.

### Example Output JSON

```json
{
  "overview": {
    "university_name": "Bucknell University",
    "location": {
      "city": "Lewisburg",
      "state": "PA",
      "country": "United States",
      "postal_code": "17837"
    },
    "contact": {
      "phone": "+1-570-577-1101",
      "email": "admissions@bucknell.edu"
    }
  },
  "tuition_breakdown": [
    {
      "fee_type": "Tuition",
      "cost": 62384,
      "currency": "USD"
    }
  ],
  "admission_deadlines": [
    {
      "deadline_type": "Early Decision",
      "deadline_date": "2025-11-15",
      "notes": "Binding agreement required."
    }
  ]
}
```

## 🧪 Testing and Validation

The `temp-scripts` folder contains various scripts used to validate each step of the pipeline.

- **Full Validation:** Runs unit and integration tests across all steps.
  ```powershell
  python temp-scripts/validate_all_steps.py
  ```
- **Edge Case Stress Test:** Runs the pipeline against deliberately challenging domains (e.g., massive JS-heavy sites, community colleges).
  ```powershell
  python temp-scripts/edge_case_test.py
  ```