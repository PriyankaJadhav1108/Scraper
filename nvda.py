"""
NVIDIA (NVDA) IR Quarterly Earnings Document Scraper
Extracts Press Releases (PDF), Presentations, Earnings Calls, and Transcripts
for the latest 2 fiscal years via NVIDIA's public IR feed API.

Output: nvda_ir_docs/earnings_index.json
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

BASE_URL = "https://investor.nvidia.com/home/default.aspx"
QUARTERLY_URL = "https://investor.nvidia.com/financial-info/quarterly-results/default.aspx"
FINANCIAL_REPORT_API = (
    "https://investor.nvidia.com/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1"
)
DOWNLOAD_DIR = Path("nvda_ir_docs")

QUARTER_MAP = {
    "first quarter": 1,
    "second quarter": 2,
    "third quarter": 3,
    "fourth quarter": 4,
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_target_fiscal_years() -> list[int]:
    """
    NVIDIA fiscal years end in late January.
    FY2027 = Feb 2026 – Jan 2027.
    """
    today = datetime.now(timezone.utc)
    current_fy = today.year if today.month == 1 else today.year + 1
    return [current_fy - 1, current_fy]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def filename_from_url(url: str, fallback: str = "document.pdf") -> str:
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    if basename and is_pdf_url(basename):
        return basename
    if basename:
        return f"{basename}.pdf"
    return fallback


def quarter_key(fiscal_year: int, quarter: int) -> str:
    return f"FY{fiscal_year} Q{quarter}"


def parse_report_quarter(report: dict) -> tuple[int, int] | None:
    fiscal_year = report.get("ReportYear")
    subtype = (report.get("ReportSubType") or "").lower()
    if not fiscal_year or subtype not in QUARTER_MAP:
        return None
    return int(fiscal_year), QUARTER_MAP[subtype]


def classify_document(title: str, url: str, category: str) -> str | None:
    label = normalize_text(title).lower()
    href = url.lower()
    cat = category.lower()

    if cat == "webcast" or label == "webcast" or "events.q4inc.com" in href:
        return "earnings_call"
    if cat == "transcript" or "transcript" in label:
        return "transcript"
    if cat == "presentation" or "presentation" in label or "quarterly-presentation" in href:
        return "presentation"
    if cat == "news" or "press release" in label:
        return "press_release"
    return None


def make_doc_entry(text: str, url: str, doc_type: str, filename: str | None = None) -> dict:
    normalized_url = url.replace("http://", "https://")

    if doc_type in {"press_release", "presentation", "transcript"}:
        file_format = "pdf"
    elif doc_type == "earnings_call":
        file_format = "webcast"
    else:
        file_format = "other"

    resolved_filename = filename
    if not resolved_filename:
        if file_format == "pdf":
            resolved_filename = filename_from_url(normalized_url)
        else:
            resolved_filename = "earnings-call-webcast"

    return {
        "text": normalize_text(text) or doc_type,
        "url": normalized_url,
        "filename": resolved_filename,
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


async def resolve_pdf_filename(page: Page, url: str) -> str:
    try:
        response = await page.request.head(url, timeout=20_000)
        if response.ok:
            disposition = response.headers.get("content-disposition", "")
            match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
            if match:
                return match.group(1)
    except Exception:
        pass
    return filename_from_url(url)


async def fetch_press_release_pdf(page: Page, newsroom_url: str) -> dict | None:
    """Collect the earnings press release PDF linked from nvidianews.nvidia.com."""
    try:
        await page.goto(newsroom_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)
        href = await page.eval_on_selector(
            'a[href*="/_gallery/download_pdf/"]',
            "el => el.href",
        )
    except Exception:
        return None

    if not href:
        return None

    pdf_url = href.replace("http://", "https://")
    filename = await resolve_pdf_filename(page, pdf_url)
    return make_doc_entry("Press Release", pdf_url, "press_release", filename=filename)


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

async def scrape_nvda_earnings(page: Page, target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching NVIDIA financial report feed")
    print(f"  URL: {FINANCIAL_REPORT_API}")
    print(f"{'─'*55}")

    financial_data = await fetch_json(page, FINANCIAL_REPORT_API)
    quarter_map: dict[str, dict] = {}
    press_release_jobs: list[tuple[str, str]] = []

    for report in financial_data.get("GetFinancialReportListResult", []):
        parsed = parse_report_quarter(report)
        if not parsed:
            continue

        fiscal_year, quarter = parsed
        if fiscal_year not in target_years:
            continue

        key = quarter_key(fiscal_year, quarter)
        if key not in quarter_map:
            quarter_map[key] = {
                "fiscal_year": fiscal_year,
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

            if doc_type == "press_release":
                press_release_jobs.append((key, url))
                continue

            if doc_type in {"presentation", "transcript"} and not is_pdf_url(url):
                continue

            entry = make_doc_entry(title, url, doc_type)
            quarter_map[key][doc_type].append(entry)

    for key, newsroom_url in press_release_jobs:
        pdf_doc = await fetch_press_release_pdf(page, newsroom_url)
        if pdf_doc:
            quarter_map[key]["press_release"].append(pdf_doc)

    output_data = []
    for fiscal_year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(fiscal_year, quarter)
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

            tag_map = {
                "press_release": "PRSS",
                "presentation": "SLID",
                "earnings_call": "CALL",
                "transcript": "TRAN",
            }
            for doc_type in ["press_release", "presentation", "earnings_call", "transcript"]:
                for doc in record[doc_type]:
                    tag = tag_map[doc_type]
                    print(f"     [{tag}] {doc['text'][:55]} → {doc['url'][:85]}")

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
        if url.lower().endswith(".pdf") or doc.get("format") == "pdf":
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
    target_years = get_target_fiscal_years()
    print(f"\n{'═'*55}")
    print("  NVDA IR Earnings Scraper")
    print(f"  Target Fiscal Years: {target_years}")
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

        print("\n  🌐 Loading NVIDIA IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_nvda_earnings(page, target_years)

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

    flat_output = finalize_company_output(output_data, "NVIDIA")
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
