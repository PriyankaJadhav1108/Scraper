"""
Shared helpers to flatten quarterly earnings records into a simple list format.
"""

from __future__ import annotations

import re

from ir_postprocess import build_title, nvda_calendar_year, process_company_items

DOC_BUCKETS = ("press_release", "presentation", "earnings_call", "webcast", "transcript")


def period_parts(record: dict, company: str) -> tuple[int, int]:
    quarter = int(record["quarter"])
    if company == "NVIDIA" and "fiscal_year" in record:
        return quarter, nvda_calendar_year(int(record["fiscal_year"]), quarter)
    if "calendar_year" in record:
        return quarter, int(record["calendar_year"])
    if "fiscal_year" in record:
        return quarter, int(record["fiscal_year"])

    label = record.get("label", "")
    calendar_match = re.match(r"(\d{4})\s+Q(\d+)", label)
    if calendar_match:
        return int(calendar_match.group(2)), int(calendar_match.group(1))

    fiscal_match = re.match(r"FY(\d{4})\s+Q(\d+)", label)
    if fiscal_match:
        fiscal_year = int(fiscal_match.group(1))
        quarter = int(fiscal_match.group(2))
        if company == "NVIDIA":
            return quarter, nvda_calendar_year(fiscal_year, quarter)
        return quarter, fiscal_year

    return quarter, 0


def resolve_item_type(bucket: str, doc: dict) -> str:
    if bucket in {"earnings_call", "webcast"}:
        return "webcast"
    if doc.get("format") in {"webcast", "audio"}:
        return "webcast"
    return bucket


def flatten_earnings_records(records: list[dict], company: str) -> list[dict]:
    items: list[dict] = []

    for record in records:
        quarter, year = period_parts(record, company)
        fiscal_year = record.get("fiscal_year")

        for bucket in DOC_BUCKETS:
            for doc in record.get(bucket, []):
                url = doc.get("url") or doc.get("link")
                if not url:
                    continue

                item_type = resolve_item_type(bucket, doc)
                text = doc.get("text", "")
                items.append(
                    {
                        "title": build_title(company, quarter, year, item_type, text),
                        "link": url,
                        "item_type": item_type,
                        "_quarter": quarter,
                        "_year": year,
                        "_fiscal_year": fiscal_year,
                        "_text": text,
                    }
                )

    return items


def finalize_company_output(records: list[dict], company: str) -> list[dict]:
    items = flatten_earnings_records(records, company)
    return process_company_items(items, company)
