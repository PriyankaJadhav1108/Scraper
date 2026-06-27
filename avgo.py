"""
Broadcom (AVGO) IR Quarterly Earnings Document Scraper

Broadcom's IR website is often unreachable from automated clients, so this
scraper uses SEC EDGAR 8-K filings (Item 2.02) and Exhibit 99.1 press releases.

Output: avgo_ir_docs/earnings_index.json
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ir_output import finalize_company_output

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE_URL = "https://investors.broadcom.com/"
QUARTERLY_URL = "https://investors.broadcom.com/financial-information/quarterly-results"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0001730168.json"
SEC_USER_AGENT = "Internship Research rahul@example.com"
DOWNLOAD_DIR = Path("avgo_ir_docs")

QUARTER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
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


def strip_html(text: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    cleaned = re.sub(r"(?s)<.*?>", " ", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned)


def parse_earnings_quarter(html: str, filing_date: str) -> tuple[int, int] | None:
    plain = strip_html(html)
    match = re.search(
        r"(first|second|third|fourth)\s+quarter(?:\s+of|\s+ended|\s+fiscal|\s+for)?",
        plain,
        re.I,
    )
    if match:
        quarter = QUARTER_WORDS[match.group(1).lower()]
        year = int(filing_date[:4])
        return year, quarter

    month_match = re.search(
        r"quarter ended\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(\d{4})",
        plain,
        re.I,
    )
    if not month_match:
        return None

    year = int(month_match.group(1))
    month_match2 = re.search(
        r"quarter ended\s+(January|February|March|April|May|June|July|August|September|October|November|December)",
        plain,
        re.I,
    )
    if not month_match2:
        return None

    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = month_map[month_match2.group(1).lower()]
    quarter = (month - 1) // 3 + 1
    return year, quarter


def find_exhibit_urls(accession_path: str) -> tuple[str | None, str | None]:
    """Return (press_release_url, presentation_url) from SEC filing index."""
    index = fetch_json(f"https://www.sec.gov/Archives/edgar/data/1730168/{accession_path}/index.json")
    items = index.get("directory", {}).get("item", [])
    press_release: str | None = None
    presentation: str | None = None

    for entry in items:
        name = entry.get("name", "")
        if not name.lower().startswith("avgo-"):
            continue
        lower = name.lower()
        url = f"https://www.sec.gov/Archives/edgar/data/1730168/{accession_path}/{name}"
        if lower.endswith(".pdf"):
            if "ex99" in lower and press_release is None:
                press_release = url
            elif any(token in lower for token in ("present", "slide", "deck")):
                presentation = url
        elif lower.endswith(".htm") and "ex99" in lower and press_release is None:
            press_release = url

    if press_release is None:
        for entry in items:
            name = entry.get("name", "")
            if name.lower().startswith("avgo-") and name.endswith(".htm"):
                if "ex" in name.lower() or "x8k" in name.lower():
                    press_release = (
                        f"https://www.sec.gov/Archives/edgar/data/1730168/{accession_path}/{name}"
                    )
                    break

    return press_release, presentation


def find_exhibit_url(accession_path: str) -> str | None:
    press_release, _ = find_exhibit_urls(accession_path)
    return press_release


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = url.replace("http://", "https://")
    if doc_type in {"press_release", "presentation"}:
        file_format = "html" if normalized_url.lower().endswith((".htm", ".html")) else "pdf"
    elif normalized_url.lower().endswith(".mp3"):
        file_format = "audio"
    elif doc_type == "webcast":
        file_format = "webcast"
    else:
        file_format = "other"

    return {
        "text": text,
        "url": normalized_url,
        "filename": normalized_url.rsplit("/", 1)[-1],
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

def scrape_broadcom_earnings(target_years: list[int]) -> list[dict]:
    print(f"\n{'─'*55}")
    print("  Fetching Broadcom SEC EDGAR submissions")
    print(f"  URL: {SEC_SUBMISSIONS_URL}")
    print(f"{'─'*55}")

    submissions = fetch_json(SEC_SUBMISSIONS_URL)
    recent = submissions["filings"]["recent"]
    quarter_map: dict[str, dict] = {}

    for index, form in enumerate(recent["form"]):
        if form != "8-K":
            continue

        filing_date = recent["filingDate"][index]
        if int(filing_date[:4]) < min(target_years):
            continue

        accession = recent["accessionNumber"][index]
        primary_document = recent["primaryDocument"][index]
        if not primary_document.startswith("avgo-"):
            continue

        accession_path = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/1730168/{accession_path}/{primary_document}"
        )
        try:
            filing_html = fetch_text(filing_url)
        except Exception as exc:
            print(f"  ⚠  Failed to fetch {filing_url}: {exc}")
            continue

        if "Item 2.02" not in filing_html:
            continue

        parsed = parse_earnings_quarter(filing_html, filing_date)
        if not parsed:
            print(f"  ⚠  Could not parse quarter for {filing_date} {primary_document}")
            continue

        year, quarter = parsed
        if year not in target_years:
            continue

        press_release_url, presentation_url = find_exhibit_urls(accession_path)
        if not press_release_url:
            print(f"  ⚠  No Exhibit 99.1 found for {filing_date}")
            continue

        key = quarter_key(year, quarter)
        if key not in quarter_map:
            quarter_map[key] = empty_quarter_record(year, quarter)

        quarter_map[key]["press_release"].append(
            make_doc_entry(
                f"Q{quarter} {year} Earnings Press Release",
                press_release_url,
                "press_release",
            )
        )
        if presentation_url:
            quarter_map[key]["presentation"].append(
                make_doc_entry(
                    f"Q{quarter} {year} Earnings Presentation",
                    presentation_url,
                    "presentation",
                )
            )
        quarter_map[key]["webcast"].append(
            make_doc_entry(
                f"Q{quarter} {year} Earnings Webcast",
                "https://investors.broadcom.com/",
                "webcast",
            )
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


def run() -> list[dict]:
    target_years = get_target_years()
    print(f"\n{'═'*55}")
    print("  AVGO IR Earnings Scraper")
    print(f"  Target Years: {target_years}")
    print(f"  Run Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"  Source: {BASE_URL}")
    print(f"{'═'*55}")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_data = scrape_broadcom_earnings(target_years)

    flat_output = finalize_company_output(output_data, "Broadcom")
    output_path = DOWNLOAD_DIR / "earnings_index.json"
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(flat_output, handle, indent=2, ensure_ascii=False)
    print(f"\n  💾 Index saved → {output_path}")

    print(f"\n{'═'*55}")
    print(f"  Total items: {len(flat_output)}")
    print(f"{'═'*55}\n")

    return flat_output


if __name__ == "__main__":
    run()
