"""
Company-specific post-processing for flattened earnings items.
"""

from __future__ import annotations

import os
from pathlib import Path

from ir_convert import convert_office_to_pdf
from ir_hosting import download_file, filename_from_url, hosted_root, publish_downloaded_asset, publish_local_asset
from ir_media import resolve_webcast_to_mp3

IR_SKIP_MEDIA = os.getenv("IR_SKIP_MEDIA", "").lower() in {"1", "true", "yes"}

ITEM_SUFFIX = {
    "press_release": "Earnings Release",
    "presentation": "Earnings Presentation",
    "webcast": "Earnings Call Webcast",
    "transcript": "Earnings Call Transcript",
}

COMPANY_RULES = {
    "Amazon": {
        "allowed": {"press_release", "presentation", "webcast", "transcript"},
    },
    "Alphabet": {
        "allowed": {"press_release", "presentation", "webcast", "transcript"},
        "webcast_to_mp3": True,
    },
    "Meta": {
        "allowed": {"press_release", "presentation", "webcast", "transcript"},
        "webcast_to_mp3": True,
    },
    "Microsoft": {
        "allowed": {"press_release", "presentation"},
        "convert_office_to_pdf": True,
    },
    "Apple": {
        "allowed": {"press_release"},
    },
    "NVIDIA": {
        "allowed": {"press_release", "presentation", "webcast", "transcript"},
        "webcast_to_mp3": True,
        "host_press_release": True,
    },
}


def nvda_calendar_year(fiscal_year: int, quarter: int) -> int:
    """NVIDIA FY26 maps to calendar 2025 data; FY27 maps to calendar 2026."""
    return fiscal_year - 1


def build_title(company: str, quarter: int, year: int, item_type: str, doc_text: str = "") -> str:
    text_lower = (doc_text or "").lower()
    if item_type == "transcript" and "follow" in text_lower:
        suffix = "Earnings Call Follow Up Transcript"
    else:
        suffix = ITEM_SUFFIX[item_type]
    return f"{company} Q{quarter} {year} {suffix}"


def _office_source_ext(url: str, item_type: str) -> str:
    lowered = url.lower()
    if lowered.endswith(".docx") or "pressrelease" in lowered:
        return "docx"
    if lowered.endswith(".pptx") or "slides" in lowered:
        return "pptx"
    return "docx" if item_type == "press_release" else "pptx"


def _process_webcast(item: dict, company: str, rules: dict) -> dict | None:
    if not rules.get("webcast_to_mp3") or IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    tmp_mp3 = hosted_root(company) / "_tmp" / f"{year}_q{quarter}_webcast.mp3"
    extracted = resolve_webcast_to_mp3(item["link"], tmp_mp3)
    if not extracted:
        print(f"    ⚠  Keeping original webcast link for {item['title']}")
        return item

    hosted_link = publish_local_asset(company, year, quarter, "webcast", extracted, "mp3")
    return {
        "title": build_title(company, quarter, year, "webcast"),
        "link": hosted_link,
        "item_type": "webcast",
    }


def _process_office_item(item: dict, company: str) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    item_type = item["item_type"]
    ext = _office_source_ext(item["link"], item_type)

    tmp_dir = hosted_root(company) / "_tmp" / f"{year}_q{quarter}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    source_name = filename_from_url(item["link"], fallback=f"{item_type}.{ext}")
    if not source_name.lower().endswith(f".{ext}"):
        source_name = f"{source_name}.{ext}"
    source_path = tmp_dir / source_name

    try:
        download_file(item["link"], source_path)
    except Exception as exc:
        print(f"    ⚠  Failed to download Office asset for {item['title']}: {exc}")
        return None

    pdf_path = convert_office_to_pdf(source_path, tmp_dir)
    if not pdf_path:
        return None

    hosted_link = publish_local_asset(company, year, quarter, item_type, pdf_path, "pdf")
    return {
        "title": build_title(company, quarter, year, item_type),
        "link": hosted_link,
        "item_type": item_type,
    }


def _process_press_release_hosting(item: dict, company: str) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    try:
        hosted_link = publish_downloaded_asset(
            company,
            year,
            quarter,
            "press_release",
            item["link"],
            "pdf",
            headers={"Referer": "https://nvidianews.nvidia.com/"},
        )
    except Exception as exc:
        print(f"    ⚠  Failed to host press release for {item['title']}: {exc}")
        return item

    return {
        "title": build_title(company, quarter, year, "press_release"),
        "link": hosted_link,
        "item_type": "press_release",
    }


def process_company_items(items: list[dict], company: str) -> list[dict]:
    rules = COMPANY_RULES.get(company, {"allowed": set(ITEM_SUFFIX)})
    allowed = rules.get("allowed", set(ITEM_SUFFIX))
    processed: list[dict] = []

    for item in items:
        if item["item_type"] not in allowed:
            continue

        current = dict(item)

        if company == "NVIDIA":
            fiscal_year = current.get("_fiscal_year", current["_year"])
            current["_year"] = nvda_calendar_year(fiscal_year, current["_quarter"])
            current["title"] = build_title(company, current["_quarter"], current["_year"], current["item_type"], current.get("_text", ""))

        if current["item_type"] == "webcast":
            current = _process_webcast(current, company, rules) or current

        elif company == "Microsoft" and rules.get("convert_office_to_pdf"):
            if current["item_type"] in {"press_release", "presentation"}:
                converted = _process_office_item(current, company)
                if converted is None:
                    continue
                current = converted

        elif company == "NVIDIA" and current["item_type"] == "press_release" and rules.get("host_press_release"):
            current = _process_press_release_hosting(current, company) or current

        processed.append(
            {
                "title": current["title"],
                "link": current["link"],
                "item_type": current["item_type"],
            }
        )

    return processed
