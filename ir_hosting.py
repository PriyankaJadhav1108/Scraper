"""
Download, convert, and publish IR assets to local hosted storage (or remote upload endpoint).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

IR_PUBLIC_BASE_URL = os.getenv("IR_PUBLIC_BASE_URL", "").rstrip("/")
IR_UPLOAD_ENDPOINT = os.getenv("IR_UPLOAD_ENDPOINT", "").strip()
IR_UPLOAD_API_KEY = os.getenv("IR_UPLOAD_API_KEY", "").strip()

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def ticker_slug(company: str) -> str:
    return {
        "Amazon": "amzn",
        "Alphabet": "goog",
        "Meta": "meta",
        "Microsoft": "msft",
        "Apple": "aapl",
        "NVIDIA": "nvda",
        "Broadcom": "avgo",
        "Oracle": "orcl",
        "Salesforce": "crm",
    }.get(company, company.lower())


def hosted_root(company: str) -> Path:
    return Path(f"{ticker_slug(company)}_ir_docs/hosted")


def asset_relative_path(company: str, year: int, quarter: int, item_type: str, ext: str) -> str:
    return f"{ticker_slug(company)}/{year}/q{quarter}/{item_type}.{ext}"


def public_url(relative_path: str) -> str:
    if IR_PUBLIC_BASE_URL:
        return f"{IR_PUBLIC_BASE_URL}/{relative_path}"
    local_path = hosted_root_from_relative(relative_path).resolve()
    return local_path.as_uri()


DEFAULT_USER_AGENT = "Internship Research rahul@example.com"


def _request(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None) -> bytes:
    merged_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        merged_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=merged_headers)
    with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=120) as response:
        return response.read()


def download_file(url: str, dest: Path, headers: dict | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = _request(url, headers=headers)
    dest.write_bytes(payload)
    return dest


def upload_file(local_path: Path, relative_path: str) -> str:
    hosted_dest = hosted_root_from_relative(relative_path)
    hosted_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, hosted_dest)

    if IR_UPLOAD_ENDPOINT:
        try:
            boundary = "----IRBoundary7MA4YWxk"
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="path"\r\n\r\n'
                f"{relative_path}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{local_path.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + local_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
            if IR_UPLOAD_API_KEY:
                headers["Authorization"] = f"Bearer {IR_UPLOAD_API_KEY}"
            _request(IR_UPLOAD_ENDPOINT, method="POST", data=body, headers=headers)
        except Exception as exc:
            print(f"    ⚠  Remote upload failed for {relative_path}: {exc}")

    return public_url(relative_path)


def hosted_root_from_relative(relative_path: str) -> Path:
    company = relative_path.split("/", 1)[0]
    return Path(f"{company}_ir_docs/hosted") / Path(*relative_path.split("/")[1:])


def filename_from_url(url: str, fallback: str = "document.bin") -> str:
    path = urlparse(url).path.rsplit("/", 1)[-1]
    return path or fallback


def publish_downloaded_asset(
    company: str,
    year: int,
    quarter: int,
    item_type: str,
    source_url: str,
    ext: str,
    headers: dict | None = None,
) -> str:
    relative = asset_relative_path(company, year, quarter, item_type, ext)
    local_tmp = hosted_root(company) / "_tmp" / relative
    download_file(source_url, local_tmp, headers=headers)
    return upload_file(local_tmp, relative)


def publish_local_asset(
    company: str,
    year: int,
    quarter: int,
    item_type: str,
    local_path: Path,
    ext: str,
) -> str:
    relative = asset_relative_path(company, year, quarter, item_type, ext)
    return upload_file(local_path, relative)
