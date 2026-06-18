"""
Apple IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, and Earnings Call materials
for the latest 2 fiscal years via Apple's public IR feed APIs.

Output: aapl_ir_docs/earnings_index.json
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

BASE_URL = "https://investor.apple.com/investor-relations/default.aspx"
FINANCIAL_REPORT_API = (
    "https://investor.apple.com/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1"
)
EVENT_API = "https://investor.apple.com/feed/Event.svc/GetEventList?LanguageId=1"
DOWNLOAD_DIR = Path("aapl_ir_docs")

QUARTER_MAP = {
    "first quarter": 1,
    "second quarter": 2,
    "third quarter": 3,
    "fourth quarter": 4,
}

EVENT_TITLE = re.compile(
    r"FY\s*(?P<fy>\d{2,4})\s*(?P<quarter>First|Second|Third|Fourth)\s*Quarter",
    re.I,
)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_target_fiscal_years() -> list[int]:
    """
    Apple fiscal years end in late September.
    FY2026 = Oct 2025 – Sep 2026.
    """
    today = datetime.now(timezone.utc)
    current_fy = today.year + 1 if today.month >= 10 else today.year
    return [current_fy - 1, current_fy]


def normalize_fy(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


def quarter_key(fiscal_year: int, quarter: int) -> str:
    return f"FY{fiscal_year} Q{quarter}"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def is_financial_statements_pdf(url: str) -> bool:
    return "consolidated_financial_statements" in url.lower()


def filename_from_url(url: str, file_format: str) -> str:
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    if basename.lower().endswith(f".{file_format}"):
        return basename
    return f"{basename}.{file_format}"


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = url.replace("http://", "https://")
    if doc_type in {"press_release", "presentation"}:
        file_format = "pdf"
    elif is_pdf_url(normalized_url):
        file_format = "pdf"
    elif "earnings-call" in normalized_url.lower():
        file_format = "webcast"
    else:
        file_format = "other"

    return {
        "text": text,
        "url": normalized_url,
        "filename": filename_from_url(normalized_url, file_format) if file_format != "webcast" else "earnings-call-webcast",
        "format": file_format,
    }


def dedupe_docs(docs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for doc in docs:
        if doc["url"] in seen:
            continue
        seen.add(doc["url"])
        unique.append(doc)
    return unique


def classify_financial_document(category: str, title: str, url: str) -> str | None:
    category = category.lower()
    title_lower = title.lower()

    if not is_pdf_url(url):
        return None
    if category == "statement" or "financial statement" in title_lower:
        return "press_release"
    if category in {"summary", "supplemental"}:
        return "presentation"
    if "press release" in title_lower or "earnings release" in title_lower:
        return "press_release"
    return None


def classify_event_attachment(title: str, url: str) -> str | None:
    combined = f"{title} {url}".lower()
    if "webcast" in combined or "earnings-call" in combined:
        return "transcript"
    if "press release" in combined:
        return "press_release"
    if "presentation" in combined:
        return "presentation"
    return None


def extract_webcast_links(event: dict) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()

    def add(url: str, text: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        links.append({"text": text, "url": url})

    webcast = event.get("WebCastLink")
    if webcast:
        add(webcast, "Conference call webcast")

    for attachment in event.get("Attachments", []):
        title = normalize_text(attachment.get("Title", ""))
        url = attachment.get("Url", "")
        if classify_event_attachment(title, url) == "transcript":
            add(url, title or "Conference call webcast")

    body = event.get("Body") or ""
    for url in re.findall(r"https?://[^\s\"'<>]*earnings-call[^\s\"'<>]*", body, re.I):
        add(url.rstrip("/") + ("/" if not url.endswith("/") else ""), "Conference call webcast")

    return links


def parse_event_quarter(title: str) -> tuple[int, int] | None:
    match = EVENT_TITLE.search(title)
    if not match:
        return None
    return normalize_fy(match.group("fy")), QUARTER_MAP[f"{match.group('quarter').lower()} quarter"]


def parse_report_quarter(report: dict) -> tuple[int, int] | None:
    fiscal_year = report.get("ReportYear")
    subtype = (report.get("ReportSubType") or "").lower()
    if not fiscal_year or subtype not in QUARTER_MAP:
        return None
    return int(fiscal_year), QUARTER_MAP[subtype]


async def fetch_newsroom_pdfs(page: Page, newsroom_url: str) -> list[dict]:
    """Collect earnings-related PDFs linked from the newsroom press release page."""
    try:
        await page.goto(newsroom_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)
        links = await page.eval_on_selector_all(
            "a[href]",
            """
            els => els.map(el => ({
                text: el.innerText.trim(),
                href: el.href
            })).filter(l => l.href && (l.href.toLowerCase().includes('.pdf') || l.href.toLowerCase().includes('/pdfs/')))
            """,
        )
    except Exception:
        return []

    pdfs: list[dict] = []
    for link in links:
        url = link["href"].replace("http://", "https://")
        if not is_pdf_url(url):
            continue
        if is_financial_statements_pdf(url):
            continue
        pdfs.append(make_doc_entry(normalize_text(link["text"]) or "press release", url, "press_release"))
    return pdfs


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

async def scrape_apple_earnings(page: Page, target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching Apple financial report feed")
    print(f"  URL: {FINANCIAL_REPORT_API}")
    print(f"{'─'*55}")

    financial_data = await fetch_json(page, FINANCIAL_REPORT_API)
    event_data = await fetch_json(page, EVENT_API)

    quarter_map: dict[str, dict] = {}

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
                "page_url": BASE_URL,
                "press_release": [],
                "presentation": [],
                "transcript": [],
                "other": [],
            }

        for doc in report.get("Documents", []):
            title = normalize_text(doc.get("DocumentTitle", ""))
            url = doc.get("DocumentPath", "")
            if not url:
                continue

            doc_type = classify_financial_document(
                doc.get("DocumentCategory", ""),
                title,
                url,
            )
            if not doc_type:
                continue

            quarter_map[key][doc_type].append(make_doc_entry(title, url, doc_type))

        newsroom_url = ""
        for doc in report.get("Documents", []):
            if (doc.get("DocumentCategory") or "").lower() == "news":
                newsroom_url = doc.get("DocumentPath", "")
                break
        if newsroom_url:
            for pdf_doc in await fetch_newsroom_pdfs(page, newsroom_url):
                quarter_map[key]["press_release"].append(pdf_doc)

    for event in event_data.get("GetEventListResult", []):
        parsed = parse_event_quarter(event.get("Title", ""))
        if not parsed:
            continue

        fiscal_year, quarter = parsed
        if fiscal_year not in target_years:
            continue

        key = quarter_key(fiscal_year, quarter)
        if key not in quarter_map:
            continue

        for attachment in event.get("Attachments", []):
            title = normalize_text(attachment.get("Title", ""))
            url = attachment.get("Url", "")
            if not url:
                continue

            doc_type = classify_event_attachment(title, url)
            if doc_type == "transcript":
                continue
            if doc_type == "press_release" and not is_pdf_url(url):
                continue
            if doc_type == "presentation" and not is_pdf_url(url):
                continue
            if doc_type:
                quarter_map[key][doc_type].append(make_doc_entry(title, url, doc_type))

    output_data = []
    for fiscal_year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(fiscal_year, quarter)
            if key not in quarter_map:
                continue

            record = quarter_map[key]
            for doc_type in ["press_release", "presentation", "transcript"]:
                record[doc_type] = dedupe_docs(record[doc_type])

            print(f"\n  ✅ {record['label']}")
            print(f"     Press Releases : {len(record['press_release'])}")
            print(f"     Presentations  : {len(record['presentation'])}")
            print(f"     Transcripts    : {len(record['transcript'])}")

            for doc_type in ["press_release", "presentation", "transcript"]:
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
    text = doc["text"][:40].strip().replace("/", "-").replace("\\", "-")
    ext = ".pdf" if ".pdf" in url.lower() else ".htm"
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
    target_years = get_target_fiscal_years()
    print(f"\n{'═'*55}")
    print("  AAPL IR Earnings Scraper")
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

        print("\n  🌐 Loading Apple IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_apple_earnings(page, target_years)

        if download_files and output_data:
            print(f"\n{'═'*55}")
            print("  📥 Downloading documents...")
            for record in output_data:
                folder = DOWNLOAD_DIR / record["label"].replace(" ", "_")
                folder.mkdir(parents=True, exist_ok=True)
                for doc_type in ["press_release", "presentation", "transcript"]:
                    for doc in record.get(doc_type, []):
                        await download_document(context, doc, folder, doc_type)

        await browser.close()

    flat_output = finalize_company_output(output_data, "Apple")
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
