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

# A full, real-looking browser header set. BOM (like most Amazon-family
# sites) is more likely to serve the full page - rather than a stripped
# down version - to something that looks like an actual browser request.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
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

    # Search results link to a title page - BOM has used both
    # /title/ttXXXXXXX/ (current, IMDb-ID based) and /release/rlXXXXXXX/
    # (older format) at different points, so match either.
    link = soup.select_one('a[href*="/title/tt"], a[href*="/release/rl"]')
    if not link:
        return None

    href = link.get("href", "")
    if href.startswith("/"):
        href = BASE_URL + href

    result_title = link.get_text(strip=True) or title
    return result_title, href


def _extract_summary_box_domestic_total(soup: BeautifulSoup) -> Optional[str]:
    """
    The top-left "All Releases" box on a title page shows Domestic /
    International / Worldwide totals, e.g.:

        <div class="a-section a-spacing-none">
            <span class="a-size-small">Domestic (<span class="percent">100%</span>)</span>
            <br/>
            <span class="a-size-medium a-text-bold"><span class="money">$953,724</span></span>
        </div>

    For titles still in theaters, this is the most current domestic total -
    more current than the per-region breakdown table further down the page,
    which can lag behind (e.g. showing a stale "Gross" figure) while a film
    is actively tracking new grosses day to day. This should be tried
    before falling back to that table.
    """
    for div in soup.select("div.a-section.a-spacing-none"):
        label_span = div.find("span", class_="a-size-small")
        if not label_span:
            continue
        if not label_span.get_text(strip=True).lower().startswith("domestic"):
            continue
        value_span = div.find("span", class_="a-size-medium")
        if not value_span:
            continue
        money = value_span.find("span", class_="money")
        if money:
            return money.get_text(strip=True)
        break  # matched the right box but no money span (e.g. "-") - don't keep scanning
    return None


def _extract_domestic_release_row(soup: BeautifulSoup) -> dict:
    """
    BOM pages include a breakdown table per region, each preceded by an
    <h3> heading ("Domestic", "Europe, Middle East, and Africa", etc.):

        <h3>Domestic</h3>
        <table>
          <tr><th>Area</th><th>Release Date</th><th>Opening</th><th>Gross</th></tr>
          <tr><td>Domestic</td><td>Jul 21, 2023</td><td>$82,455,420</td><td>$330,078,895</td></tr>
        </table>

    This is the authoritative source for both the domestic release date
    and the domestic lifetime gross, and pulling them from the same row
    avoids accidentally grabbing an unrelated date (BOM also lists an
    "Earliest Release Date" elsewhere on the page, which can reflect an
    international release instead of the domestic one).
    """
    release_date, domestic_total = None, None

    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True).lower() != "domestic":
            continue
        table = h3.find_next("table")
        if not table:
            continue
        data_row = table.find_all("tr")[1] if len(table.find_all("tr")) > 1 else None
        if not data_row:
            continue
        cells = data_row.find_all("td")
        if len(cells) >= 4:
            release_date = cells[1].get_text(" ", strip=True)
            domestic_total = cells[3].get_text(strip=True)
        break

    return {"release_date": release_date, "domestic_total": domestic_total}


def _extract_by_release_rollout(soup: BeautifulSoup) -> Optional[str]:
    """
    Fallback for titles that don't have a clean per-region "Domestic" table
    (common on older titles, or ones that opened limited-then-wide rather
    than day-and-date worldwide). These instead have a "By Release" table:

        <h3>By Release</h3>
        <table>
          <tr><th>Release Group</th><th>Rollout</th><th>Markets</th>...</tr>
          <tr><td>Original Release</td><td>August 19-January 1, 2010</td>...</tr>
        </table>

    Returns the "Rollout" value from the first data row (the original
    release, as opposed to any later re-release rows). Note this can be a
    date *range* rather than a single date, since it reflects a limited-to-
    wide rollout rather than a simultaneous release.
    """
    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True).lower() != "by release":
            continue
        table = h3.find_next("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if "rollout" not in header_cells:
            continue
        rollout_idx = header_cells.index("rollout")

        data_cells = rows[1].find_all("td")
        if len(data_cells) > rollout_idx:
            return data_cells[rollout_idx].get_text(" ", strip=True)
        break

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

    # --- Release date + Domestic total ---
    domestic_row = _extract_domestic_release_row(soup)
    release_date = domestic_row["release_date"]

    # Domestic total priority:
    #  1. Top "All Releases" summary box - most current, especially for
    #     titles still actively tracking new box office day to day.
    #  2. Per-region "Domestic" table's Gross column - can lag behind #1
    #     for very recent/still-running titles.
    #  3. First "money" span on the page - last-resort fallback.
    domestic_total = _extract_summary_box_domestic_total(soup)
    if domestic_total is None:
        domestic_total = domestic_row["domestic_total"]

    # Fallback 1: older / limited-then-wide titles often lack the clean
    # per-region "Domestic" table above, but have a "By Release" rollout
    # table instead.
    if release_date is None:
        release_date = _extract_by_release_rollout(soup)

    # Fallback 2: if nothing above found a domestic total, fall back to the
    # first "money" span on the page (typically the domestic lifetime
    # total in the summary table).
    if domestic_total is None:
        money_spans = soup.select("span.money")
        if money_spans:
            domestic_total = money_spans[0].get_text(strip=True)

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
