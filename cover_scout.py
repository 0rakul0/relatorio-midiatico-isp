"""Cover Scout: audit newspaper edition pages using an authorized browser session.

Install: pip install playwright && python -m playwright install chromium
First run: python cover_scout.py --login-only
The collector never stores passwords, bypasses access controls, or asserts that
a webpage text match proves an appearance on the printed front page.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

KEYWORDS = {
    "direct": [r"\bISP(?:-RJ)?\b", r"instituto de seguran[cç]a p[úu]blica"],
    "study": [r"dossi[eê] mulher", r"mapa da mulher carioca"],
    "indirect": [r"letalidade violenta", r"roubos? de carga", r"roubos? de ve[ií]culos?", r"viol[eê]ncia contra (?:a|as) mulher(?:es)?"],
}


def classify(text: str) -> tuple[str, list[str]]:
    found = []
    for category, patterns in KEYWORDS.items():
        for pattern in patterns:
            if re.search(pattern, text or "", flags=re.I):
                found.append(f"{category}:{pattern}")
    if any(x.startswith("direct:") for x in found):
        return "CANDIDATE_DIRECT", found
    if any(x.startswith("study:") for x in found):
        return "CANDIDATE_STUDY", found
    if found:
        return "CANDIDATE_INDIRECT", found
    return "NO_TEXT_MATCH", []


def candidate_dates(publication_date: date, days_before: int = 1, days_after: int = 3):
    return [publication_date + timedelta(days=offset) for offset in range(-days_before, days_after + 1)]


def load_news(path: Path) -> list[dict]:
    """Input CSV columns: id,date,title,url (ISO YYYY-MM-DD dates)."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    for record in records:
        date.fromisoformat(record["date"])
    return records


def write_jsonl(path: Path, item: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def _authorized(url: str, allowed_hosts: set[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts
    )


def inspect_page(page, url: str, output: Path, *, allowed_hosts: set[str]):
    if not _authorized(url, allowed_hosts):
        raise ValueError("URL outside authorized newspaper domains")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1200)
    if not _authorized(page.url, allowed_hosts):
        raise ValueError("Navigation redirected outside authorized domains")
    text = page.locator("body").inner_text(timeout=15000)
    status, matches = classify(text)
    record = {
        "url": page.url, "title": page.title(), "status": status,
        "matched_patterns": matches,
        "page_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "manual_review": "PENDING",
        "evidence_type": "WEB_PAGE_TEXT_NOT_VERIFIED_COVER",
    }
    write_jsonl(output / "inspection.jsonl", record)
    return record


def main():
    parser = argparse.ArgumentParser(description="Authorized newspaper cover evidence collector")
    parser.add_argument("--news-csv", type=Path, help="CSV with id,date,title,url")
    parser.add_argument("--edition-urls", type=Path, help="Text file with authorized edition URLs")
    parser.add_argument("--profile", type=Path, default=Path(".cover_scout_browser"))
    parser.add_argument("--output", type=Path, default=Path("cover_scout_output"))
    parser.add_argument("--login-url", default="https://oglobo.globo.com/")
    parser.add_argument("--login-only", action="store_true")
    args = parser.parse_args()
    if args.news_csv:
        records = load_news(args.news_csv)
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / "candidate_dates.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["news_id", "news_date", "candidate_edition_date", "title", "url"])
            for item in records:
                for day in candidate_dates(date.fromisoformat(item["date"])):
                    writer.writerow([item["id"], item["date"], day.isoformat(), item["title"], item["url"]])
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(args.profile), headless=False, accept_downloads=False
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if args.login_only or not args.edition_urls:
                page.goto(args.login_url)
                input("Authenticate manually if needed; press Enter when ready... ")
            if args.edition_urls:
                for url in args.edition_urls.read_text(encoding="utf-8").splitlines():
                    url = url.strip()
                    if not url or url.startswith("#"):
                        continue
                    try:
                        result = inspect_page(
                            page, url, args.output, allowed_hosts={"oglobo.globo.com"}
                        )
                        print(result["status"], url)
                    except Exception as exc:
                        write_jsonl(args.output / "errors.jsonl", {"url": url, "error": str(exc)})
                        print("ERROR", url, str(exc))
        finally:
            context.close()


if __name__ == "__main__":
    main()
