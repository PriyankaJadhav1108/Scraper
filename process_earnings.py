"""
Run all IR earnings scrapers and write post-processed flat JSON outputs.
"""

from __future__ import annotations

import asyncio
import importlib


SCRAPERS = [
    ("amzn", "Amazon"),
    ("goog", "Alphabet"),
    ("meta", "Meta"),
    ("msft", "Microsoft"),
    ("aapl", "Apple"),
    ("nvda", "NVIDIA"),
]


async def run_all(download_files: bool = False) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}
    for module_name, company in SCRAPERS:
        print(f"\n{'#' * 60}\n  Running {company} ({module_name}.py)\n{'#' * 60}")
        module = importlib.import_module(module_name)
        results[company] = await module.main(download_files=download_files)
    return results


if __name__ == "__main__":
    asyncio.run(run_all(download_files=False))
