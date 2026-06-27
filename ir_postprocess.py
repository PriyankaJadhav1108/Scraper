"""
Company-specific post-processing for flattened earnings items.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

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
    "Tesla": {
        "allowed": {"press_release", "presentation", "webcast"},
    },
    "Oracle": {
        "allowed": {"press_release", "presentation", "webcast"},
    },
    "Salesforce": {
        "allowed": {"press_release", "presentation", "webcast"},
    },
    "Broadcom": {
        "allowed": {"press_release", "presentation", "webcast"},
        "convert_html_to_pdf": True,
        "host_pdf": True,
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
    if lowered.endswith(".htm") or lowered.endswith(".html"):
        return "html"
    if lowered.endswith(".docx") or "pressrelease" in lowered:
        return "docx"
    if lowered.endswith(".pptx") or "slides" in lowered:
        return "pptx"
    return "docx" if item_type == "press_release" else "pptx"


def _local_path_from_uri(uri: str) -> Path | None:
    if not uri.startswith("file://"):
        return None
    return Path(unquote(urlparse(uri).path))


def _process_webcast(item: dict, company: str, rules: dict) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    link = item["link"]

    if link.lower().split("?", 1)[0].endswith(".mp3"):
        if not rules.get("host_mp3") and not rules.get("webcast_to_mp3"):
            return item
        try:
            hosted_link = publish_downloaded_asset(
                company, year, quarter, "webcast", link, "mp3"
            )
            return {
                "title": build_title(company, quarter, year, "webcast"),
                "link": hosted_link,
                "item_type": "webcast",
            }
        except Exception as exc:
            print(f"    ⚠  Failed to host MP3 webcast for {item['title']}: {exc}")
            return item

    if not rules.get("webcast_to_mp3"):
        return item

    tmp_mp3 = hosted_root(company) / "_tmp" / f"{year}_q{quarter}_webcast.mp3"
    extracted = resolve_webcast_to_mp3(link, tmp_mp3)
    if not extracted:
        print(f"    ⚠  Keeping original webcast link for {item['title']}")
        return item

    hosted_link = publish_local_asset(company, year, quarter, "webcast", extracted, "mp3")
    return {
        "title": build_title(company, quarter, year, "webcast"),
        "link": hosted_link,
        "item_type": "webcast",
    }


def _process_pdf_item(item: dict, company: str) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    item_type = item["item_type"]
    try:
        hosted_link = publish_downloaded_asset(
            company, year, quarter, item_type, item["link"], "pdf"
        )
    except Exception as exc:
        print(f"    ⚠  Failed to host PDF for {item['title']}: {exc}")
        return None

    return {
        "title": build_title(company, quarter, year, item_type),
        "link": hosted_link,
        "item_type": item_type,
    }


def _process_download_convert_item(item: dict, company: str) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]
    item_type = item["item_type"]
    link = item["link"]

    if link.lower().split("?", 1)[0].endswith(".pdf"):
        return _process_pdf_item(item, company)

    ext = _office_source_ext(link, item_type)
    tmp_dir = hosted_root(company) / "_tmp" / f"{year}_q{quarter}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    source_name = filename_from_url(link, fallback=f"{item_type}.{ext}")
    if not source_name.lower().endswith(f".{ext}"):
        source_name = f"{source_name}.{ext}"
    source_path = tmp_dir / source_name

    try:
        download_file(link, source_path)
    except Exception as exc:
        print(f"    ⚠  Failed to download asset for {item['title']}: {exc}")
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


def _process_transcript_mp3(item: dict, company: str, webcast_mp3: Path | None) -> dict | None:
    if IR_SKIP_MEDIA:
        return item

    year = item["_year"]
    quarter = item["_quarter"]

    if item["link"].lower().split("?", 1)[0].endswith(".mp3"):
        try:
            hosted_link = publish_downloaded_asset(
                company, year, quarter, "transcript", item["link"], "mp3"
            )
            return {
                "title": build_title(company, quarter, year, "transcript"),
                "link": hosted_link,
                "item_type": "transcript",
            }
        except Exception as exc:
            print(f"    ⚠  Failed to host transcript MP3 for {item['title']}: {exc}")
            return None

    if webcast_mp3 and webcast_mp3.exists():
        tmp_copy = hosted_root(company) / "_tmp" / f"{year}_q{quarter}_transcript.mp3"
        tmp_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(webcast_mp3, tmp_copy)
        hosted_link = publish_local_asset(company, year, quarter, "transcript", tmp_copy, "mp3")
        return {
            "title": build_title(company, quarter, year, "transcript"),
            "link": hosted_link,
            "item_type": "transcript",
        }

    print(f"    ⚠  No MP3 transcript source for {item['title']}")
    return None


def process_company_items(items: list[dict], company: str) -> list[dict]:
    rules = COMPANY_RULES.get(company, {"allowed": set(ITEM_SUFFIX)})
    allowed = rules.get("allowed", set(ITEM_SUFFIX))

    webcast_mp3_paths: dict[tuple[int, int], Path] = {}
    if rules.get("webcast_to_mp3") and not IR_SKIP_MEDIA:
        for item in items:
            if item["item_type"] != "webcast" or item["item_type"] not in allowed:
                continue
            processed = _process_webcast(item, company, rules)
            if processed:
                local_path = _local_path_from_uri(processed["link"])
                if local_path and local_path.exists() and local_path.suffix.lower() == ".mp3":
                    webcast_mp3_paths[(item["_year"], item["_quarter"])] = local_path

    processed: list[dict] = []

    for item in items:
        if item["item_type"] not in allowed:
            continue

        current = dict(item)

        if company == "NVIDIA":
            fiscal_year = current.get("_fiscal_year", current["_year"])
            current["_year"] = nvda_calendar_year(fiscal_year, current["_quarter"])
            current["title"] = build_title(
                company,
                current["_quarter"],
                current["_year"],
                current["item_type"],
                current.get("_text", ""),
            )

        if current["item_type"] == "webcast":
            if rules.get("webcast_to_mp3"):
                key = (current["_year"], current["_quarter"])
                if key in webcast_mp3_paths:
                    current = {
                        "title": build_title(company, current["_quarter"], current["_year"], "webcast"),
                        "link": webcast_mp3_paths[key].resolve().as_uri(),
                        "item_type": "webcast",
                    }
                else:
                    converted = _process_webcast(current, company, rules)
                    if converted:
                        current = converted
            elif current["link"].lower().endswith(".mp3"):
                converted = _process_webcast(current, company, rules)
                if converted:
                    current = converted

        elif current["item_type"] in {"press_release", "presentation"}:
            if company == "Microsoft" and rules.get("convert_office_to_pdf"):
                converted = _process_download_convert_item(current, company)
                if converted is None:
                    continue
                current = converted
            elif company == "Broadcom" and (
                rules.get("convert_html_to_pdf") or rules.get("host_pdf")
            ):
                if current["link"].lower().split("?", 1)[0].endswith(".pdf"):
                    converted = _process_pdf_item(current, company)
                else:
                    converted = _process_download_convert_item(current, company)
                if converted is None:
                    continue
                current = converted
            elif company == "NVIDIA" and current["item_type"] == "press_release" and rules.get("host_press_release"):
                converted = _process_press_release_hosting(current, company)
                if converted:
                    current = converted
            elif rules.get("host_pdf"):
                converted = _process_pdf_item(current, company)
                if converted is None:
                    continue
                current = converted

        elif current["item_type"] == "transcript":
            if (
                rules.get("host_pdf")
                and current["link"].lower().split("?", 1)[0].endswith(".pdf")
            ):
                converted = _process_pdf_item(current, company)
                if converted is None:
                    continue
                current = converted
            elif current["link"].lower().split("?", 1)[0].endswith(".mp3"):
                if rules.get("host_mp3") or rules.get("transcript_mp3_from_webcast"):
                    webcast_mp3 = webcast_mp3_paths.get((current["_year"], current["_quarter"]))
                    converted = _process_transcript_mp3(current, company, webcast_mp3)
                    if converted is None:
                        continue
                    current = converted
            elif rules.get("transcript_mp3_from_webcast"):
                webcast_mp3 = webcast_mp3_paths.get((current["_year"], current["_quarter"]))
                converted = _process_transcript_mp3(current, company, webcast_mp3)
                if converted is None:
                    continue
                current = converted

        processed.append(
            {
                "title": current["title"],
                "link": current["link"],
                "item_type": current["item_type"],
            }
        )

    return processed
