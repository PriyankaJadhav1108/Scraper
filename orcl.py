"""
Oracle (ORCL) IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, Earnings Call Webcasts, and Transcripts
for the latest 2 calendar years via Oracle's public IR feed API.

Output: orcl_ir_docs/earnings_index.json
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from ir_output import finalize_company_output
from q4_ir import get_target_years, scrape_financial_report_api

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://investor.oracle.com/home/default.aspx"
QUARTERLY_URL = "https://investor.oracle.com/financials/quarterly-results/default.aspx"
FINANCIAL_REPORT_API = (
    "https://investor.oracle.com/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1"
)
DOWNLOAD_DIR = Path("orcl_ir_docs")

# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

async def scrape_oracle_earnings(page, target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching Oracle financial report feed")
    print(f"  URL: {FINANCIAL_REPORT_API}")
    print(f"{'─'*55}")

    output_data = await scrape_financial_report_api(
        page, FINANCIAL_REPORT_API, QUARTERLY_URL, target_years
    )

    for record in output_data:
        print(f"\n  ✅ {record['label']}")
        print(f"     Press Releases : {len(record['press_release'])}")
        print(f"     Presentations  : {len(record['presentation'])}")
        print(f"     Webcasts       : {len(record['webcast'])}")
        print(f"     Transcripts    : {len(record['transcript'])}")
        for doc_type in ["press_release", "presentation", "webcast", "transcript"]:
            for doc in record.get(doc_type, []):
                print(f"     [{doc_type.upper()[:4]}] {doc['text'][:55]} → {doc['url'][:85]}")

    return output_data


async def main(download_files: bool = False) -> list[dict]:
    target_years = get_target_years()
    print(f"\n{'═'*55}")
    print("  ORCL IR Earnings Scraper")
    print(f"  Target Years: {target_years}")
    print(f"  Run Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'═'*55}")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_data: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print("\n  🌐 Loading Oracle IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_oracle_earnings(page, target_years)
        await browser.close()

    flat_output = finalize_company_output(output_data, "Oracle")
    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(flat_output, handle, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(f"  Total items: {len(flat_output)}")
    print(f"{'═'*55}\n")

    return flat_output


if __name__ == "__main__":
    asyncio.run(main(download_files=False))
