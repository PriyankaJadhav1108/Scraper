"""
Alphabet (GOOG) IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, Earnings Calls, and Transcripts
for the latest 2 calendar years via abc.xyz public IR feed APIs.

Output: goog_ir_docs/earnings_index.json
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, BrowserContext, Page

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://abc.xyz/investor/"
EARNINGS_URL = "https://abc.xyz/investor/Earnings/default.aspx"
FINANCIAL_REPORT_API = (
    "https://abc.xyz/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1"
)
EVENT_API = "https://abc.xyz/feed/Event.svc/GetEventList?LanguageId=1"
SITE_ORIGIN = "https://abc.xyz"
DOWNLOAD_DIR = Path("goog_ir_docs")

QUARTER_MAP = {
    "first quarter": 1,
    "second quarter": 2,
    "third quarter": 3,
    "fourth quarter": 4,
}

EARNINGS_EVENT_RE = re.compile(r"(\d{4})\s+Q([1-4])\s+Earnings", re.I)

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


def is_youtube_url(href: str) -> bool:
    lowered = href.lower()
    return "youtube.com" in lowered or "youtu.be" in lowered


def normalize_url(url: str) -> str:
    if url.startswith("/"):
        return f"{SITE_ORIGIN}{url}"
    return url.replace("http://", "https://")


def normalize_pdf_url(url: str) -> str:
    normalized = normalize_url(url)
    if is_pdf_url(normalized):
        return normalized
    if "transcript" in normalized.lower() or "earnings-release" in normalized.lower():
        return f"{normalized.rstrip('/')}.pdf"
    return normalized


def filename_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def classify_financial_document(title: str, url: str, category: str) -> str | None:
    label = normalize_text(title).lower()
    href = url.lower()
    cat = category.lower()

    if cat == "news" or "earnings release" in label:
        return "press_release"
    if cat == "presentation" or "earnings slides" in label:
        return "presentation"
    # webcast entries point to event pages; Event API supplies direct links
    if cat == "webcast":
        return None
    return None


def quarter_key(year: int, quarter: int) -> str:
    return f"{year} Q{quarter}"


def parse_report_quarter(report: dict) -> tuple[int, int] | None:
    fiscal_year = report.get("ReportYear")
    subtype = (report.get("ReportSubType") or "").lower()
    if not fiscal_year or subtype not in QUARTER_MAP:
        return None
    return int(fiscal_year), QUARTER_MAP[subtype]


def parse_event_quarter(title: str) -> tuple[int, int] | None:
    match = EARNINGS_EVENT_RE.search(title)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = normalize_url(url)

    if doc_type in {"press_release", "presentation", "transcript"}:
        normalized_url = normalize_pdf_url(normalized_url)
        if not is_pdf_url(normalized_url):
            return {}
        file_format = "pdf"
    elif doc_type == "earnings_call":
        if not is_youtube_url(normalized_url):
            return {}
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


def empty_quarter_record(year: int, quarter: int) -> dict:
    return {
        "calendar_year": year,
        "quarter": quarter,
        "label": quarter_key(year, quarter),
        "page_url": EARNINGS_URL,
        "press_release": [],
        "presentation": [],
        "earnings_call": [],
        "transcript": [],
        "other": [],
    }


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

async def scrape_financial_reports(page: Page, target_years: list[int], quarter_map: dict) -> None:
    print(f"\n{'─'*55}")
    print("  Fetching Alphabet financial report feed")
    print(f"  URL: {FINANCIAL_REPORT_API}")
    print(f"{'─'*55}")

    financial_data = await fetch_json(page, FINANCIAL_REPORT_API)

    for report in financial_data.get("GetFinancialReportListResult", []):
        parsed = parse_report_quarter(report)
        if not parsed:
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = empty_quarter_record(year, quarter)

        for doc in report.get("Documents", []):
            title = normalize_text(doc.get("DocumentTitle", ""))
            url = doc.get("DocumentPath", "")
            if not url:
                continue

            doc_type = classify_financial_document(
                title, url, doc.get("DocumentCategory", "")
            )
            if not doc_type:
                continue

            entry = make_doc_entry(title, url, doc_type)
            if entry:
                quarter_map[key][doc_type].append(entry)


async def scrape_event_feed(page: Page, target_years: list[int], quarter_map: dict) -> None:
    print(f"\n{'─'*55}")
    print("  Fetching Alphabet event feed")
    print(f"  URL: {EVENT_API}")
    print(f"{'─'*55}")

    event_data = await fetch_json(page, EVENT_API)

    for event in event_data.get("GetEventListResult", []):
        title = normalize_text(event.get("Title", ""))
        parsed = parse_event_quarter(title)
        if not parsed:
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = empty_quarter_record(year, quarter)

        for attachment in event.get("Attachments", []):
            att_title = normalize_text(attachment.get("Title", ""))
            att_url = attachment.get("Url", "")
            if not att_url:
                continue

            att_label = att_title.lower()
            att_href = att_url.lower()

            if is_youtube_url(att_url) or "webcast" in att_label:
                entry = make_doc_entry(att_title, att_url, "earnings_call")
                if entry:
                    quarter_map[key]["earnings_call"].append(entry)
            elif "transcript" in att_label or "transcript" in att_href:
                entry = make_doc_entry(att_title, att_url, "transcript")
                if entry:
                    quarter_map[key]["transcript"].append(entry)


async def scrape_goog_earnings(page: Page, target_years: list[int]) -> list[dict]:
    quarter_map: dict[str, dict] = {}

    await scrape_financial_reports(page, target_years, quarter_map)
    await scrape_event_feed(page, target_years, quarter_map)

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
    print("  ALPHABET (GOOG) IR Earnings Scraper")
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

        print("\n  🌐 Loading Alphabet IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_goog_earnings(page, target_years)

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

    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(
        f"  {'Quarter':<12} {'Press Rel':>10} {'Present':>10}"
        f" {'Earnings':>10} {'Transcript':>12}"
    )
    print(f"  {'─'*56}")
    for record in output_data:
        print(
            f"  {record['label']:<12}"
            f" {len(record['press_release']):>10}"
            f" {len(record['presentation']):>10}"
            f" {len(record['earnings_call']):>10}"
            f" {len(record['transcript']):>12}"
        )
    print(f"{'═'*55}\n")

    return output_data


if __name__ == "__main__":
    asyncio.run(main(download_files=False))
