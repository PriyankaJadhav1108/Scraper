"""
Convert Microsoft Office documents to PDF for hosted delivery.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_libreoffice() -> str | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def convert_office_to_pdf(source_path: Path, output_dir: Path) -> Path | None:
    soffice = find_libreoffice()
    if not soffice:
        print("    ⚠  LibreOffice not found; cannot convert Office files to PDF.")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(source_path)],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except Exception as exc:
        print(f"    ⚠  Office conversion failed for {source_path.name}: {exc}")
        return None

    pdf_path = output_dir / f"{source_path.stem}.pdf"
    return pdf_path if pdf_path.exists() else None
