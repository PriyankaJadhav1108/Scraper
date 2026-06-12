"""
Amazon IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, and Earnings Call Transcripts
for the latest 2 calendar years from the quarterly results page.

Output: amzn_ir_docs/earnings_index.json
"""

import asyncio
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright, BrowserContext

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://ir.aboutamazon.com/overview/default.aspx"
QUARTERLY_URL = "https://ir.aboutamazon.com/quarterly-results/default.aspx"
DOWNLOAD_DIR = Path("amzn_ir_docs")

QUARTER_NAMES = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
}

CDN_PATH = re.compile(r"/(\d{4})/q(\d)/", re.I)
NEWS_RELEASE = re.compile(
    r"news-release-details/(\d{4})/Amazon[-.]com-Announces-(First|Second|Third|Fourth)-Quarter",
    re.I,
)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_target_years() -> list[int]:
    """Return the current calendar year and the previous year."""
    year = datetime.now(timezone.utc).year
    return [year - 1, year]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip().lower()


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def classify_link(text: str, href: str) -> str | None:
    """Map a link to press_release, presentation, or transcript."""
    label = normalize_text(text)
    href_lower = href.lower()

    if (
        is_pdf_url(href)
        and (
            "earnings release (pdf)" in label
            or "earnings-release" in href_lower
            or "/earnings-result/" in href_lower
        )
    ):
        return "press_release"

    if (
        is_pdf_url(href)
        and (
            "presentation" in label
            or "webslides" in href_lower
            or "/presentation/" in href_lower
        )
    ):
        return "presentation"

    if label == "webcast" or "full-call" in href_lower or "earnings-report" in href_lower:
        return "transcript"
    return None


def quarter_key(year: int, quarter: int) -> str:
    return f"{year} Q{quarter}"


def parse_quarter_from_href(href: str) -> tuple[int, int] | None:
    match = CDN_PATH.search(href)
    if match:
        return int(match.group(1)), int(match.group(2))

    match = NEWS_RELEASE.search(href)
    if match:
        announcement_year = int(match.group(1))
        quarter = QUARTER_NAMES[match.group(2).lower()]
        # Q4 results are usually announced in the following calendar year.
        if quarter == 4 and announcement_year > 2000:
            return announcement_year - 1, quarter
        return announcement_year, quarter

    return None


def filename_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = url.replace("http://", "https://")
    if doc_type in {"press_release", "presentation"}:
        file_format = "pdf"
    elif normalized_url.lower().endswith(".mp3"):
        file_format = "audio"
    elif is_pdf_url(normalized_url):
        file_format = "pdf"
    else:
        file_format = "other"

    return {
        "text": normalize_text(text) or doc_type,
        "url": normalized_url,
        "filename": filename_from_url(normalized_url),
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


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

async def scrape_quarterly_results(page, target_years: list[int]) -> list[dict]:
    """Scrape the quarterly results hub and group documents by quarter."""
    print(f"\n{'─'*55}")
    print("  Scraping Amazon quarterly results page")
    print(f"  URL: {QUARTERLY_URL}")
    print(f"{'─'*55}")

    response = await page.goto(QUARTERLY_URL, wait_until="domcontentloaded", timeout=30_000)
    if response and response.status >= 400:
        raise RuntimeError(f"Failed to load quarterly results page (HTTP {response.status})")

    await page.wait_for_timeout(3000)

    links = await page.eval_on_selector_all(
        "a[href]",
        """
        els => els.map(el => ({
            text: el.innerText.trim(),
            href: el.href
        }))
        """,
    )

    quarter_map: dict[str, dict] = {}

    for link in links:
        href = link["href"]
        text = link["text"]
        if not href or href.startswith("mailto:"):
            continue

        parsed = parse_quarter_from_href(href)
        if not parsed:
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        doc_type = classify_link(text, href)
        if not doc_type:
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = {
                "calendar_year": year,
                "quarter": quarter,
                "label": f"{year} Q{quarter}",
                "page_url": QUARTERLY_URL,
                "press_release": [],
                "presentation": [],
                "transcript": [],
                "other": [],
            }

        quarter_map[key][doc_type].append(make_doc_entry(text, href, doc_type))

    output_data = []
    for year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(year, quarter)
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
    """Download a single document if it's a direct file link."""
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
    target_years = get_target_years()
    print(f"\n{'═'*55}")
    print("  AMZN IR Earnings Scraper")
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
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print("\n  🌐 Loading Amazon IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        output_data = await scrape_quarterly_results(page, target_years)

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

    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(f"  {'Quarter':<12} {'Press Rel':>10} {'Present':>10} {'Transcript':>12}")
    print(f"  {'─'*46}")
    for record in output_data:
        print(
            f"  {record['label']:<12}"
            f" {len(record['press_release']):>10}"
            f" {len(record['presentation']):>10}"
            f" {len(record['transcript']):>12}"
        )
    print(f"{'═'*55}\n")

    return output_data


if __name__ == "__main__":
    asyncio.run(main(download_files=False))
