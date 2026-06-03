"""
University ETL Pipeline — Main Entry Point
============================================

Add university domains to the UNIVERSITIES list below.
Each entry can be a bare domain, a www domain, or a full URL.
The pipeline will normalize them automatically.

Output:
  - JSON files saved to ./output/ directory
  - Logs saved to temp-scripts/runs.log
"""

import asyncio
import os

from src.pipeline import ETLPipeline


# ══════════════════════════════════════════════════════
# PUT YOUR UNIVERSITY DOMAINS HERE
# ══════════════════════════════════════════════════════
UNIVERSITIES = [
    "bucknell.edu",
    "udc.edu",
    "salisbury.edu",
]

# ── Configuration ──
OUTPUT_DIR = "output"
LOG_FILE = os.path.join("temp-scripts", "runs.log")
HEADLESS = False  # Set to True for headless mode (no browser window)


async def main():
    pipeline = ETLPipeline(headless=HEADLESS)
    results = await pipeline.run_batch(
        domains=UNIVERSITIES,
        output_dir=OUTPUT_DIR,
        log_file=LOG_FILE,
    )
    return results


if __name__ == "__main__":
    asyncio.run(main())
