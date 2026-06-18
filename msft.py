"""
Microsoft IR Quarterly Earnings Document Scraper
Extracts Press Releases, Presentations, and Transcripts
for the latest 2 fiscal years dynamically.

Output: msft_ir_docs/earnings_index.json
"""

import asyncio
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from playwright.async_api import async_playwright, Page, BrowserContext

from ir_output import finalize_company_output

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://www.microsoft.com/en-us/investor"
EARNINGS_BASE = f"{BASE_URL}/earnings"
DOWNLOAD_DIR = Path("msft_ir_docs")

# Microsoft uses fiscal years (FY ends June 30).
# FY2026 = July 2025 – June 2026
# Quarters: Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun

DOC_TYPES = ["press-release", "presentation", "transcript"]

# Document link patterns to look for on each earnings page
LINK_PATTERNS = {
    "transcript": re.compile(r"transcript|call.?transcript|webcast", re.I),
}

MSFT_CDN = "cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/"

# ─────────────────────────────────────────────
# FISCAL YEAR HELPERS
# ─────────────────────────────────────────────

def get_target_fiscal_years() -> list[int]:
    """
    Dynamically determine the 2 most recent fiscal years.
    Microsoft's FY ends June 30.
    - Before July 1 of calendar year X  → current FY = X
    - On/after July 1 of calendar year X → current FY = X+1
    Returns e.g. [2025, 2026] when run in 2026.
    """
    today = datetime.now(timezone.utc)
    # FY number = calendar year if month >= 7 else calendar year (same year)
    # FY starts July 1: if month >= 7, we're IN that FY already
    current_fy = today.year if today.month >= 7 else today.year
    # Adjust: FY2026 ends June 2026, so if today < July 2026, current FY = 2026
    # More precisely: FY = year the FY ends (June 30 of that year)
    if today.month >= 7:
        current_fy = today.year + 1  # started new FY
    else:
        current_fy = today.year      # still in FY that ends this June
    return [current_fy - 1, current_fy]


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def resolve_document_url(href: str) -> str:
    """Resolve Office Online viewer links to the underlying CDN asset."""
    normalized = href.replace("http://", "https://")
    if "view.officeapps.live.com" in normalized and "src=" in normalized:
        src = parse_qs(urlparse(normalized).query).get("src", [""])[0]
        if src:
            return unquote(src)
    return normalized


CONTENT_TYPE_FORMAT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}

DEFAULT_FORMAT_BY_DOC_TYPE = {
    "press_release": "docx",
    "presentation": "pptx",
    "transcript": "docx",
}


def format_from_content_type(content_type: str, doc_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_FORMAT.get(content_type, DEFAULT_FORMAT_BY_DOC_TYPE.get(doc_type, "other"))


def filename_from_url(url: str, file_format: str) -> str:
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    if basename.lower().endswith(f".{file_format}"):
        return basename
    return f"{basename}.{file_format}"


def is_msft_document_asset(url: str) -> bool:
    url_lower = url.lower()
    return is_pdf_url(url_lower) or MSFT_CDN in url_lower


def classify_msft_document(text: str, href: str) -> str | None:
    resolved = resolve_document_url(href)
    combined = f"{text} {href} {resolved}".lower()
    short_link = href.lower()

    if "aka.ms" in short_link:
        if "pressrelease" in short_link:
            return "press_release"
        if "slides" in short_link:
            return "presentation"
        if "transcript" in short_link:
            return "transcript"
        return None

    if not is_msft_document_asset(resolved):
        return None
    if any(skip in combined for skip in ["financialstatement", "outlook", "_10q", "10-q", "product%20list", "product list"]):
        return None
    if "press release" in combined or "pressrelease" in combined:
        return "press_release"
    if "earnings call slides" in combined or "slides" in combined:
        return "presentation"
    if LINK_PATTERNS["transcript"].search(combined):
        return "transcript"
    return None


async def resolve_msft_url(page: Page, href: str) -> str:
    normalized = href.replace("http://", "https://")
    if "aka.ms" not in normalized.lower():
        return resolve_document_url(normalized)
    try:
        await page.goto(normalized, wait_until="domcontentloaded", timeout=20_000)
        return resolve_document_url(page.url)
    except Exception:
        return normalized


async def detect_file_format(page: Page, url: str, doc_type: str) -> str:
    try:
        response = await page.request.head(url, timeout=20_000)
        if response.ok:
            return format_from_content_type(response.headers.get("content-type", ""), doc_type)
    except Exception:
        pass
    return DEFAULT_FORMAT_BY_DOC_TYPE.get(doc_type, "other")


async def make_doc_entry(page: Page, text: str, href: str, doc_type: str) -> dict:
    resolved = await resolve_msft_url(page, href)
    file_format = await detect_file_format(page, resolved, doc_type)
    return {
        "text": text.strip() or doc_type,
        "url": resolved,
        "filename": filename_from_url(resolved, file_format),
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


def build_earnings_urls(fiscal_years: list[int]) -> list[dict]:
    """
    Build all quarter URLs for the given fiscal years.
    URL pattern: /en-us/investor/earnings/fy-{YEAR}-q{1-4}/default.aspx
    """
    quarters = []
    for fy in fiscal_years:
        for q in range(1, 5):
            quarters.append({
                "fiscal_year": fy,
                "quarter": q,
                "label": f"FY{fy} Q{q}",
                "url": f"{EARNINGS_BASE}/fy-{fy}-q{q}/press-release-webcast",
            })
    return quarters


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

async def scrape_quarter(
    page: Page,
    quarter_info: dict,
    output_data: list,
) -> None:
    """Navigate to a quarterly earnings page and extract document links."""
    label = quarter_info["label"]
    url   = quarter_info["url"]

    print(f"\n{'─'*55}")
    print(f"  Scraping: {label}")
    print(f"  URL:      {url}")
    print(f"{'─'*55}")

    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        if response and response.status == 404:
            print(f"  ⚠  Page not yet available (404) — quarter may not have occurred yet.")
            return

        # Wait for main content to load
        await page.wait_for_timeout(2000)

        # Try to close any cookie/consent banners
        for selector in ["button#onetrust-accept-btn-handler", "[aria-label='Accept']", ".close-button"]:
            try:
                btn = page.locator(selector)
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        quarter_docs = {
            "fiscal_year":   quarter_info["fiscal_year"],
            "quarter":       quarter_info["quarter"],
            "label":         label,
            "page_url":      url,
            "press_release": [],
            "presentation":  [],
            "transcript":    [],
            "other":         [],
        }

        # ── Strategy 1: Grab all <a> tags and classify by text/href ──
        links = await page.eval_on_selector_all(
            "a[href]",
            """
            els => els.map(el => ({
                text: el.innerText.trim(),
                href: el.href,
                title: el.title || '',
                ariaLabel: el.getAttribute('aria-label') || ''
            }))
            """
        )

        for link in links:
            href = link["href"]
            text = link["text"]
            combined = f"{text} {link['title']} {link['ariaLabel']} {href}".lower()

            # Skip empty, nav, or non-document links
            if not href or href in ("#", url):
                continue
            if href.startswith("mailto:") or "facebook.com" in href or "linkedin.com" in href:
                continue
            if href.split("#")[0].rstrip("/").endswith("press-release-webcast"):
                continue

            doc_type = classify_msft_document(text, href)
            if not doc_type:
                continue

            quarter_docs[doc_type].append(await make_doc_entry(page, text, href, doc_type))

        # ── Strategy 2: Look for section headings + nearby links ──
        # Some MSFT IR pages use tab/section layouts
        try:
            sections = await page.eval_on_selector_all(
                "[class*='tab'], [class*='section'], [class*='card'], [role='tabpanel']",
                """
                els => els.map(el => ({
                    heading: (el.querySelector('h2,h3,h4,strong') || {}).innerText || '',
                    links: Array.from(el.querySelectorAll('a[href]')).map(a => ({
                        text: a.innerText.trim(),
                        href: a.href
                    }))
                }))
                """
            )
            for section in sections:
                heading = section.get("heading", "").lower()
                for link in section.get("links", []):
                    href = link["href"]
                    text = link["text"]
                    if not href:
                        continue
                    doc_type = classify_msft_document(f"{heading} {text}", href)
                    if not doc_type:
                        continue
                    entry = await make_doc_entry(page, text, href, doc_type)
                    if entry not in quarter_docs[doc_type]:
                        quarter_docs[doc_type].append(entry)
        except Exception:
            pass

        for doc_type in ["press_release", "presentation", "transcript"]:
            quarter_docs[doc_type] = dedupe_docs(quarter_docs[doc_type])

        # ── Print summary ──
        print(f"  ✅ Press Releases : {len(quarter_docs['press_release'])}")
        print(f"  ✅ Presentations  : {len(quarter_docs['presentation'])}")
        print(f"  ✅ Transcripts    : {len(quarter_docs['transcript'])}")
        if quarter_docs["other"]:
            print(f"  ℹ  Other PDFs     : {len(quarter_docs['other'])}")

        for doc_type in ["press_release", "presentation", "transcript"]:
            for doc in quarter_docs[doc_type]:
                print(f"     [{doc_type.upper()[:4]}] {doc['text'][:60]} → {doc['url'][:80]}")

        output_data.append(quarter_docs)

    except Exception as e:
        print(f"  ❌ Error scraping {label}: {e}")


# ─────────────────────────────────────────────
# OPTIONAL: PDF DOWNLOADER
# ─────────────────────────────────────────────

async def download_document(
    context: BrowserContext,
    doc: dict,
    dest_folder: Path,
    doc_type: str,
) -> None:
    """Download a single document if it's a direct PDF/file link."""
    url  = doc["url"]
    text = doc.get("filename") or doc["text"][:40].strip().replace("/", "-").replace("\\", "-")
    ext  = f".{doc.get('format', 'pdf')}" if doc.get("format") else (".pdf" if ".pdf" in url.lower() else ".htm")
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


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def main(download_files: bool = False):
    fiscal_years = get_target_fiscal_years()
    print(f"\n{'═'*55}")
    print(f"  MSFT IR Earnings Scraper")
    print(f"  Target Fiscal Years: {fiscal_years}")
    print(f"  Run Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'═'*55}")

    quarters = build_earnings_urls(fiscal_years)
    output_data = []

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            locale="en-US",
        )
        page = await context.new_page()

        # Pre-visit main IR page to warm up cookies/session
        print("\n  🌐 Loading Microsoft IR landing page...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        for quarter_info in quarters:
            await scrape_quarter(page, quarter_info, output_data)
            await asyncio.sleep(1.5)  # polite delay

        # ── Optional: download all found documents ──
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

    # ── Save JSON index ──
    flat_output = finalize_company_output(output_data, "Microsoft")
    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(flat_output, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(f"  Total items: {len(flat_output)}")
    print(f"{'═'*55}\n")

    return flat_output


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Set download_files=True to also download PDFs/HTMLs locally
    asyncio.run(main(download_files=False))
