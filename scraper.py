"""
Box Office Mojo scraper
------------------------
Given a movie title, finds its Box Office Mojo page and extracts:
  - Title (as listed on BOM)
  - Release date
  - Domestic box office total (lifetime, to date)

Design notes:
- BOM has no public API, so this works in two steps per title:
    1. Hit BOM's search endpoint to find the matching /release/rlXXXXXXXXXX/ page
    2. Fetch that page and parse the summary block for release date + domestic gross
- BOM pages are server-rendered HTML (no JavaScript needed), so plain
  requests + BeautifulSoup is enough - no headless browser required.
- Selectors below reflect BOM's post-2019 page layout. Sites redesign
  occasionally, so if a field comes back empty for a title you know is on
  the site, that's the first thing to check (see the docstring at the
  bottom of `parse_release_page` for how to debug it quickly).
"""

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.boxofficemojo.com"
SEARCH_URL = BASE_URL + "/search/?q={query}"

# A real browser User-Agent. BOM (like most Amazon-family sites) is more
# likely to serve normal HTML to something that looks like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 15   # seconds
DELAY_BETWEEN_REQUESTS = 1.5  # seconds - be polite, don't hammer the site


@dataclass
class BoxOfficeResult:
    query: str                     # what you searched for
    title: Optional[str] = None    # title as found on BOM
    release_date: Optional[str] = None
    domestic_total: Optional[str] = None
    url: Optional[str] = None
    status: str = "not_found"      # "ok" | "not_found" | "error"
    error: Optional[str] = None


def _get(url: str) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


def find_release_url(title: str) -> Optional[tuple]:
    """
    Search Box Office Mojo for a title and return (bom_title, url) for the
    best (first) match, or None if nothing came back.
    """
    search_url = SEARCH_URL.format(query=quote(title))
    resp = _get(search_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Search results are rows linking to /release/rlXXXXXXXXXX/
    link = soup.select_one('a[href*="/release/rl"]')
    if not link:
        return None

    href = link.get("href", "")
    if href.startswith("/"):
        href = BASE_URL + href

    result_title = link.get_text(strip=True) or title
    return result_title, href


def _extract_summary_field(soup: BeautifulSoup, label: str) -> Optional[str]:
    """
    BOM's release page has a right-hand "Summary" block where each fact is
    a label/value pair, e.g.:
        <div class="a-section a-spacing-none">
            <span class="a-size-small">Release Date</span>
            <span class="a-size-medium">...</span>
        </div>
    This walks every summary row, and returns the value next to the given
    label (case-insensitive, partial match).
    """
    for row in soup.select("div.mojo-summary-values > div"):
        spans = row.find_all("span")
        if len(spans) >= 2:
            row_label = spans[0].get_text(strip=True)
            row_value = spans[-1].get_text(" ", strip=True)
            if label.lower() in row_label.lower():
                return row_value
    return None


def parse_release_page(html: str) -> dict:
    """
    Parse a Box Office Mojo /release/rlXXXXXXXXXX/ page.

    Returns dict with: title, release_date, domestic_total

    If BOM has changed its markup and a field isn't found, this degrades
    gracefully (returns None for that field) rather than throwing - the
    caller can decide how to handle partial results.

    Debugging tip if fields come back empty: fetch one page, save
    resp.text to a local .html file, open it in a browser, right-click the
    field you want (e.g. the domestic total number) -> Inspect, and see
    what class/structure wraps it now. Update the selectors above to match.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Title ---
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    # --- Domestic total ---
    # The performance summary table at the top of the page lists Domestic /
    # International / Worldwide totals in that order, each as a span with
    # class "money".
    domestic_total = None
    money_spans = soup.select("span.money")
    if money_spans:
        domestic_total = money_spans[0].get_text(strip=True)

    # --- Release date ---
    release_date = _extract_summary_field(soup, "Release Date")

    return {
        "title": title,
        "release_date": release_date,
        "domestic_total": domestic_total,
    }


def lookup_title(title: str) -> BoxOfficeResult:
    """
    Full pipeline for a single title: search -> fetch -> parse.
    Never raises - errors are captured on the result object so batch runs
    don't die on one bad title.
    """
    result = BoxOfficeResult(query=title)
    try:
        found = find_release_url(title)
        if not found:
            result.status = "not_found"
            return result

        bom_title, url = found
        result.url = url

        page = _get(url)
        parsed = parse_release_page(page.text)

        result.title = parsed["title"] or bom_title
        result.release_date = parsed["release_date"]
        result.domestic_total = parsed["domestic_total"]
        result.status = "ok"

    except requests.exceptions.RequestException as e:
        result.status = "error"
        result.error = f"Network error: {e}"
    except Exception as e:
        result.status = "error"
        result.error = str(e)

    return result


def lookup_titles(titles: list, delay: float = DELAY_BETWEEN_REQUESTS, progress_callback=None):
    """
    Batch version. Takes a list of title strings, returns a list of
    BoxOfficeResult, pausing `delay` seconds between requests to be polite
    to BOM's servers.

    progress_callback(i, total, title) is called before each lookup if
    provided - handy for a UI progress bar.
    """
    results = []
    total = len(titles)
    for i, title in enumerate(titles):
        title = title.strip()
        if not title:
            continue
        if progress_callback:
            progress_callback(i, total, title)
        logger.info(f"Looking up ({i+1}/{total}): {title}")
        results.append(lookup_title(title))
        if i < total - 1:
            time.sleep(delay)
    return results


if __name__ == "__main__":
    # Quick manual test - run `python scraper.py "Oppenheimer" "Barbie"`
    import sys
    test_titles = sys.argv[1:] or ["Oppenheimer", "Barbie"]
    for r in lookup_titles(test_titles):
        print(r)
