"""
Meta IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, Earnings Calls, and Transcripts
for the latest 2 calendar years via Meta's public IR feed API.

Output: meta_ir_docs/earnings_index.json
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, BrowserContext, Page

from ir_output import finalize_company_output

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://investor.atmeta.com/home/default.aspx"
QUARTERLY_URL = "https://investor.atmeta.com/financials/quarterly-earnings/default.aspx"
FINANCIAL_REPORT_API = (
    "https://investor.atmeta.com/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1"
)
DOWNLOAD_DIR = Path("meta_ir_docs")

QUARTER_MAP = {
    "first quarter": 1,
    "second quarter": 2,
    "third quarter": 3,
    "fourth quarter": 4,
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_target_years() -> list[int]:
    """Return the current calendar year and the previous year."""
    year = datetime.now(timezone.utc).year
    return [year - 1, year]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def normalize_pdf_url(url: str) -> str:
    """Ensure transcript/release PDF URLs end with .pdf when Meta omits the extension."""
    normalized = url.replace("http://", "https://")
    if is_pdf_url(normalized):
        return normalized
    if "transcript" in normalized.lower() or "exhibit-99" in normalized.lower() or "earnings-presentation" in normalized.lower():
        return f"{normalized.rstrip('/')}.pdf"
    return normalized


def filename_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def classify_document(title: str, url: str, category: str) -> str | None:
    label = normalize_text(title).lower()
    href = url.lower()
    cat = category.lower()

    if cat == "webcast" or label == "webcast" or "events.q4inc.com" in href:
        return "earnings_call"
    if "earnings release" in label or "exhibit-99" in href:
        return "press_release"
    if "earnings slides" in label or "earnings-presentation" in href or "earnings presentation" in label:
        return "presentation"
    if "transcript" in label:
        return "transcript"
    return None


def quarter_key(year: int, quarter: int) -> str:
    return f"{year} Q{quarter}"


def parse_report_quarter(report: dict) -> tuple[int, int] | None:
    fiscal_year = report.get("ReportYear")
    subtype = (report.get("ReportSubType") or "").lower()
    if not fiscal_year or subtype not in QUARTER_MAP:
        return None
    return int(fiscal_year), QUARTER_MAP[subtype]


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = normalize_pdf_url(url)

    if doc_type in {"press_release", "presentation", "transcript"}:
        if not is_pdf_url(normalized_url):
            return {}
        file_format = "pdf"
    elif doc_type == "earnings_call":
        file_format = "webcast"
    else:
        file_format = "other"

    return {
        "text": normalize_text(text) or doc_type,
        "url": normalized_url,
        "filename": filename_from_url(normalized_url) if file_format == "pdf" else "earnings-call-webcast",
        "format": file_format,
    }


def dedupe_docs(docs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for doc in docs:
        if not doc or doc.get("url") in seen:
            continue
        seen.add(doc["url"])
        unique.append(doc)
    return unique


async def fetch_json(page: Page, url: str) -> dict:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    if response and response.status >= 400:
        raise RuntimeError(f"Failed to fetch {url} (HTTP {response.status})")

    pre = await page.query_selector("pre")
    if pre:
        return json.loads(await pre.inner_text())

    body_text = await page.eval_on_selector("body", "el => el.innerText.trim()")
    return json.loads(body_text)


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

async def scrape_meta_earnings(page: Page, target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching Meta financial report feed")
    print(f"  URL: {FINANCIAL_REPORT_API}")
    print(f"{'─'*55}")

    financial_data = await fetch_json(page, FINANCIAL_REPORT_API)
    quarter_map: dict[str, dict] = {}

    for report in financial_data.get("GetFinancialReportListResult", []):
        parsed = parse_report_quarter(report)
        if not parsed:
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = {
                "calendar_year": year,
                "quarter": quarter,
                "label": key,
                "page_url": QUARTERLY_URL,
                "press_release": [],
                "presentation": [],
                "earnings_call": [],
                "transcript": [],
                "other": [],
            }

        for doc in report.get("Documents", []):
            title = normalize_text(doc.get("DocumentTitle", ""))
            url = doc.get("DocumentPath", "")
            if not url:
                continue

            doc_type = classify_document(title, url, doc.get("DocumentCategory", ""))
            if not doc_type:
                continue

            entry = make_doc_entry(title, url, doc_type)
            if entry:
                quarter_map[key][doc_type].append(entry)

    output_data = []
    for year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(year, quarter)
            if key not in quarter_map:
                continue

            record = quarter_map[key]
            for doc_type in ["press_release", "presentation", "earnings_call", "transcript"]:
                record[doc_type] = dedupe_docs(record[doc_type])

            print(f"\n  ✅ {record['label']}")
            print(f"     Press Releases : {len(record['press_release'])}")
            print(f"     Presentations  : {len(record['presentation'])}")
            print(f"     Earnings Calls : {len(record['earnings_call'])}")
            print(f"     Transcripts    : {len(record['transcript'])}")

            for doc_type in ["press_release", "presentation", "earnings_call", "transcript"]:
                for doc in record[doc_type]:
                    print(f"     [{doc_type.upper()[:4]}] {doc['text'][:55]} → {doc['url'][:85]}")

            output_data.append(record)

    return output_data


async def download_document(
    context: BrowserContext,
    doc: dict,
    dest_folder: Path,
    doc_type: str,
) -> None:
    url = doc["url"]
    text = doc.get("filename") or doc["text"][:40].strip().replace("/", "-").replace("\\", "-")
    ext = ".pdf" if doc.get("format") == "pdf" else ".htm"
    filename = f"{doc_type}_{text}{ext}"
    dest = dest_folder / filename

    if dest.exists():
        print(f"    ⏭  Already downloaded: {filename}")
        return

    try:
        page = await context.new_page()
        if url.lower().endswith(".pdf"):
            async with context.expect_download() as dl_info:
                await page.goto(url, timeout=30_000)
            download = await dl_info.value
            await download.save_as(dest)
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            content = await page.content()
            dest.with_suffix(".html").write_text(content, encoding="utf-8")
        print(f"    ⬇  Downloaded: {filename}")
        await page.close()
    except Exception as e:
        print(f"    ❌ Download failed for {url}: {e}")


async def main(download_files: bool = False):
    target_years = get_target_years()
    print(f"\n{'═'*55}")
    print("  META IR Earnings Scraper")
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
            accept_downloads=True,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print("\n  🌐 Loading Meta IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_meta_earnings(page, target_years)

        if download_files and output_data:
            print(f"\n{'═'*55}")
            print("  📥 Downloading documents...")
            for record in output_data:
                folder = DOWNLOAD_DIR / record["label"].replace(" ", "_")
                folder.mkdir(parents=True, exist_ok=True)
                for doc_type in ["press_release", "presentation", "earnings_call", "transcript"]:
                    for doc in record.get(doc_type, []):
                        await download_document(context, doc, folder, doc_type)

        await browser.close()

    flat_output = finalize_company_output(output_data, "Meta")
    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(flat_output, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(f"  Total items: {len(flat_output)}")
    print(f"{'═'*55}\n")

    return flat_output


if __name__ == "__main__":
    asyncio.run(main(download_files=False))
