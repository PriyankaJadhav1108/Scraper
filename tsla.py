"""
Tesla (TSLA) IR Quarterly Earnings Document Scraper

Tesla's IR HTML pages are bot-protected, so this scraper uses:
  1. SEC EDGAR 8-K filings (Item 2.02) to identify earnings quarters
  2. Direct PDF URLs on ir.tesla.com/_flysystem/s3/sec/
  3. Optional presentation PDFs on digitalassets.tesla.com
  4. Webcast replay pages with embedded YouTube audio

Output: tsla_ir_docs/earnings_index.json
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ir_output import finalize_company_output

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://ir.tesla.com/#quarterly-disclosure"
QUARTERLY_URL = "https://ir.tesla.com/#quarterly-disclosure"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0001318605.json"
SEC_USER_AGENT = "Internship Research rahul@example.com"
DOWNLOAD_DIR = Path("tsla_ir_docs")

QUARTER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
}

QUARTER_SLUGS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
}

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_target_years() -> list[int]:
    year = datetime.now(timezone.utc).year
    return [year - 1, year]


def quarter_key(year: int, quarter: int) -> str:
    return f"{year} Q{quarter}"


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url))


def url_exists(url: str) -> bool:
    try:
        result = subprocess.run(
            ["curl", "-sI", "-A", "Mozilla/5.0", url],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        return " 200 " in first_line or first_line.endswith(" 200")
    except Exception:
        return False


def earnings_pdf_url(accession: str, primary_document: str) -> str:
    acc_path = accession.replace("-", "")
    base = primary_document.replace(".htm", "")
    return f"https://ir.tesla.com/_flysystem/s3/sec/{acc_path}/{base}-gen.pdf"


def presentation_pdf_url(year: int, quarter: int) -> str:
    return (
        "https://digitalassets.tesla.com/tesla-contents/image/upload/"
        f"IR/TSLA-Q{quarter}-{year}-Update.pdf"
    )


def webcast_url(filing_date: str) -> str:
    return f"https://ir.tesla.com/webcast-{filing_date}"


def filename_from_url(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def parse_earnings_quarter(html: str, filing_date: str) -> tuple[int, int] | None:
    match = re.search(r"(First|Second|Third|Fourth) Quarter", html, re.I)
    if not match:
        return None

    quarter = QUARTER_WORDS[match.group(1).lower()]
    year = int(filing_date[:4])
    if quarter == 4 and int(filing_date[5:7]) <= 2:
        year -= 1
    return year, quarter


def is_earnings_8k(html: str, primary_document: str) -> bool:
    if not primary_document.lower().startswith("tsla-"):
        return False
    return "Item 2.02" in html or "Results of Operations and Financial Condition" in html


def resolve_tesla_webcast_audio(webcast_page_url: str) -> str:
    """Extract YouTube watch URL from Tesla webcast replay page."""
    try:
        html = fetch_text(webcast_page_url)
    except Exception:
        return webcast_page_url

    match = re.search(r"youtube\.com/embed/([^\"'?]+)", html, re.I)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return webcast_page_url


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = url.replace("http://", "https://")
    if doc_type in {"press_release", "presentation", "transcript"}:
        file_format = "pdf"
    elif normalized_url.lower().endswith(".mp3") or "youtube.com" in normalized_url.lower():
        file_format = "audio"
    elif doc_type == "webcast":
        file_format = "webcast"
    else:
        file_format = "other"

    return {
        "text": text,
        "url": normalized_url,
        "filename": filename_from_url(normalized_url) if file_format == "pdf" else "earnings-call-webcast",
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


def empty_quarter_record(year: int, quarter: int) -> dict:
    return {
        "calendar_year": year,
        "quarter": quarter,
        "label": quarter_key(year, quarter),
        "page_url": QUARTERLY_URL,
        "press_release": [],
        "presentation": [],
        "webcast": [],
        "transcript": [],
        "other": [],
    }


# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────

def scrape_tesla_earnings(target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching Tesla SEC EDGAR submissions")
    print(f"  URL: {SEC_SUBMISSIONS_URL}")
    print(f"{'─'*55}")

    submissions = fetch_json(SEC_SUBMISSIONS_URL)
    recent = submissions["filings"]["recent"]
    quarter_map: dict[str, dict] = {}

    for index, form in enumerate(recent["form"]):
        if form != "8-K":
            continue

        filing_date = recent["filingDate"][index]
        filing_year = int(filing_date[:4])
        if filing_year < min(target_years) - 1 or filing_year > max(target_years) + 1:
            continue

        accession = recent["accessionNumber"][index]
        primary_document = recent["primaryDocument"][index]
        acc_path = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/1318605/{acc_path}/{primary_document}"
        )

        try:
            html = fetch_text(filing_url)
        except Exception as exc:
            print(f"  ⚠  Failed to fetch {filing_url}: {exc}")
            continue

        if not is_earnings_8k(html, primary_document):
            continue

        parsed = parse_earnings_quarter(html, filing_date)
        if not parsed:
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = empty_quarter_record(year, quarter)

        update_pdf = earnings_pdf_url(accession, primary_document)
        quarter_map[key]["press_release"].append(
            make_doc_entry(f"Q{quarter} {year} Shareholder Update", update_pdf, "press_release")
        )

        presentation_url = presentation_pdf_url(year, quarter)
        if url_exists(presentation_url):
            quarter_map[key]["presentation"].append(
                make_doc_entry(f"Q{quarter} {year} Update Deck", presentation_url, "presentation")
            )
        else:
            quarter_map[key]["presentation"].append(
                make_doc_entry(f"Q{quarter} {year} Update Deck", update_pdf, "presentation")
            )

        webcast_page = webcast_url(filing_date)
        audio_url = resolve_tesla_webcast_audio(webcast_page)
        quarter_map[key]["webcast"].append(
            make_doc_entry(f"Q{quarter} {year} Earnings Webcast", audio_url, "webcast")
        )

    output_data: list[dict] = []
    for year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(year, quarter)
            if key not in quarter_map:
                continue

            record = quarter_map[key]
            for doc_type in ["press_release", "presentation", "webcast", "transcript"]:
                record[doc_type] = dedupe_docs(record[doc_type])

            print(f"\n  ✅ {record['label']}")
            print(f"     Press Releases : {len(record['press_release'])}")
            print(f"     Presentations  : {len(record['presentation'])}")
            print(f"     Webcasts       : {len(record['webcast'])}")
            print(f"     Transcripts    : {len(record['transcript'])}")

            for doc_type in ["press_release", "presentation", "webcast", "transcript"]:
                for doc in record[doc_type]:
                    print(f"     [{doc_type.upper()[:4]}] {doc['text'][:55]} → {doc['url'][:85]}")

            output_data.append(record)

    return output_data


async def main(download_files: bool = False) -> list[dict]:
    target_years = get_target_years()
    print(f"\n{'═'*55}")
    print("  TSLA IR Earnings Scraper")
    print(f"  Target Years: {target_years}")
    print(f"  Run Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"  Source: {BASE_URL}")
    print(f"{'═'*55}")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_data = scrape_tesla_earnings(target_years)

    flat_output = finalize_company_output(output_data, "Tesla")
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
