"""
Shared helpers for Q4-platform investor relations scrapers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page

from ir_media import resolve_q4_recording_url

QUARTER_MAP = {
    "first quarter": 1,
    "second quarter": 2,
    "third quarter": 3,
    "fourth quarter": 4,
}


def get_target_years() -> list[int]:
    year = datetime.now(timezone.utc).year
    return [year - 1, year]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def is_pdf_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".pdf")


def is_mp3_url(href: str) -> bool:
    return href.lower().split("?", 1)[0].endswith(".mp3")


def normalize_pdf_url(url: str) -> str:
    normalized = url.replace("http://", "https://")
    if is_pdf_url(normalized):
        return normalized
    lowered = normalized.lower()
    if any(token in lowered for token in ("transcript", "pressrelease", "press-release", "presentation", "investor-deck", "earnings-release")):
        return f"{normalized.rstrip('/')}.pdf"
    return normalized


def filename_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def quarter_key(year: int, quarter: int) -> str:
    return f"{year} Q{quarter}"


def parse_report_quarter(report: dict) -> tuple[int, int] | None:
    report_year = report.get("ReportYear")
    subtype = (report.get("ReportSubType") or "").lower()
    if not report_year or subtype not in QUARTER_MAP:
        return None
    return int(report_year), QUARTER_MAP[subtype]


def empty_quarter_record(year: int, quarter: int, page_url: str) -> dict:
    return {
        "calendar_year": year,
        "quarter": quarter,
        "label": quarter_key(year, quarter),
        "page_url": page_url,
        "press_release": [],
        "presentation": [],
        "webcast": [],
        "transcript": [],
        "other": [],
    }


def classify_q4_document(title: str, url: str, category: str) -> str | None:
    label = normalize_text(title).lower()
    href = url.lower()
    cat = category.lower()

    if "customer wins" in label or "oracle.com/customers" in href:
        return None
    if cat == "news" or "press release" in label or "earnings release" in label or "pressrelease" in href:
        return "press_release"
    if cat == "presentation" or "presentation" in label or "investor deck" in label or "slides" in label:
        return "presentation"
    if cat in {"webcast", "online"} or label == "webcast" or "events.q4inc.com" in href or "open-exchange.net" in href:
        return "webcast"
    if "transcript" in label or "transcript" in href:
        return "transcript"
    return None


def make_doc_entry(text: str, url: str, doc_type: str) -> dict:
    normalized_url = url.replace("http://", "https://")

    if doc_type in {"press_release", "presentation", "transcript"}:
        normalized_url = normalize_pdf_url(normalized_url)
        if not is_pdf_url(normalized_url):
            return {}
        file_format = "pdf"
    elif doc_type == "webcast":
        if is_mp3_url(normalized_url):
            file_format = "audio"
        else:
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
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if response and response.status >= 400:
        raise RuntimeError(f"Failed to fetch {url} (HTTP {response.status})")

    pre = await page.query_selector("pre")
    if pre:
        return json.loads(await pre.inner_text())

    body_text = await page.eval_on_selector("body", "el => el.innerText.trim()")
    return json.loads(body_text)


def enrich_webcast_mp3(docs: list[dict]) -> list[dict]:
    """Replace Q4 attendee links with direct MP3 URLs when published."""
    enriched: list[dict] = []
    for doc in docs:
        url = doc.get("url", "")
        if "events.q4inc.com/attendee" in url:
            resolved = resolve_q4_recording_url(url)
            if resolved:
                mp3_url, _ = resolved
                enriched.append(
                    {
                        **doc,
                        "url": mp3_url,
                        "format": "audio",
                        "filename": mp3_url.rsplit("/", 1)[-1],
                    }
                )
                continue
        enriched.append(doc)
    return enriched


def add_transcript_mp3_from_webcast(record: dict) -> None:
    """When no transcript exists, mirror the earnings-call MP3 like Amazon full-call links."""
    has_transcript = bool(record.get("transcript"))
    if has_transcript:
        return
    for doc in record.get("webcast", []):
        if doc.get("format") == "audio" or doc.get("url", "").lower().split("?", 1)[0].endswith(".mp3"):
            record["transcript"].append(
                {
                    "text": "earnings call transcript",
                    "url": doc["url"],
                    "filename": doc.get("filename", "earnings-call-transcript.mp3"),
                    "format": "audio",
                }
            )
            return


async def scrape_financial_report_api(
    page: Page,
    api_url: str,
    quarterly_url: str,
    target_years: list[int],
) -> list[dict]:
    financial_data = await fetch_json(page, api_url)
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
            quarter_map[key] = empty_quarter_record(year, quarter, quarterly_url)

        for doc in report.get("Documents", []):
            title = normalize_text(doc.get("DocumentTitle", ""))
            url = doc.get("DocumentPath", "")
            if not url:
                continue

            doc_type = classify_q4_document(title, url, doc.get("DocumentCategory", ""))
            if not doc_type:
                continue

            entry = make_doc_entry(title, url, doc_type)
            if entry:
                quarter_map[key][doc_type].append(entry)

    output_data: list[dict] = []
    for year in sorted(target_years):
        for quarter in range(1, 5):
            key = quarter_key(year, quarter)
            if key not in quarter_map:
                continue

            record = quarter_map[key]
            for doc_type in ["press_release", "presentation", "webcast", "transcript"]:
                record[doc_type] = dedupe_docs(record[doc_type])
            record["webcast"] = dedupe_docs(enrich_webcast_mp3(record["webcast"]))
            output_data.append(record)

    return output_data
