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
import datetime
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
    tt_code: Optional[str] = None  # IMDb tt-code, e.g. "tt15398776"
    release_date: Optional[str] = None
    domestic_total: Optional[str] = None
    widest_release: Optional[str] = None
    weeks_in_theaters: Optional[str] = None
    prev_weekend_gross: Optional[str] = None
    prev_weekend_date: Optional[str] = None
    prev_weekend_theaters: Optional[str] = None
    last_recorded_date: Optional[str] = None
    last_recorded_gross: Optional[str] = None
    url: Optional[str] = None
    status: str = "not_found"      # "ok" | "not_found" | "error"
    error: Optional[str] = None


# A persistent session (rather than one-off requests.get calls) so cookies
# carry across the several requests made per title - closer to how an
# actual browser behaves when navigating from page to page on the same
# site, and less likely to look automated to bot-detection systems that
# watch for repeated cold/cookie-less requests in quick succession.
_session = requests.Session()
_session.headers.update(HEADERS)


def _get(url: str) -> requests.Response:
    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


def _get_with_retry(url: str, retries: int = 2, backoff: float = 2.0) -> requests.Response:
    """
    Same as _get, but retries on failure with increasing delay. Used for
    the secondary fetches (weekend/daily/release-group pages) that happen
    after a couple of other requests already went out for the same title -
    these seem more prone to transient blocks/timeouts than the first
    request, likely bot-protection reacting to a quick burst of requests.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _get(url)
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_error


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
          <tr><td><a href="/release/rlXXXXXXXXX/?...">Domestic</a></td>
              <td>Jul 21, 2023</td><td>$82,455,420</td><td>$330,078,895</td></tr>
        </table>

    This is the authoritative source for the domestic release date, the
    domestic lifetime gross, AND the base release URL (used to reach the
    title's own weekend/daily performance tables - see
    `fetch_weekend_summary` / `fetch_daily_summary` below). Pulling date +
    gross from the same row avoids accidentally grabbing an unrelated date
    (BOM also lists an "Earliest Release Date" elsewhere on the page, which
    can reflect an international release instead of the domestic one).
    """
    release_date, domestic_total, release_base_url = None, None, None
    domestic_section_found = False

    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True).lower() != "domestic":
            continue
        domestic_section_found = True
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
            link = cells[0].find("a")
            if link and link.get("href"):
                href = link["href"].split("?")[0]
                if href.startswith("/"):
                    href = BASE_URL + href
                if not href.endswith("/"):
                    href += "/"
                release_base_url = href
        break

    return {
        "release_date": release_date,
        "domestic_total": domestic_total,
        "domestic_section_found": domestic_section_found,
        "release_base_url": release_base_url,
    }


def _extract_summary_label_value(soup: BeautifulSoup, label: str) -> Optional[str]:
    """
    Generic reader for simple label/value rows in the summary panel, e.g.:
        <div class="a-section a-spacing-none">
            <span>Widest Release</span><span>1,000 theaters</span>
        </div>
    Matches on exact label text (case-insensitive).
    """
    for div in soup.select("div.a-section.a-spacing-none"):
        spans = div.find_all("span", recursive=False)
        if len(spans) == 2 and spans[0].get_text(strip=True).lower() == label.lower():
            return spans[1].get_text(" ", strip=True)
    return None


def _extract_by_release_info(soup: BeautifulSoup) -> dict:
    """
    Fallback for titles that don't have a clean per-region "Domestic" table
    (common on older titles, or ones that opened limited-then-wide rather
    than day-and-date worldwide). These instead have a "By Release" table:

        <h3>By Release</h3>
        <table>
          <tr><th>Release Group</th><th>Rollout</th><th>Markets</th>...</tr>
          <tr><td><a href="/releasegroup/grXXXXXXXXXX/">Original Release</a></td>
              <td>August 19-January 1, 2010</td>...</tr>
        </table>

    Returns both:
      - "rollout": the date *range* from the Rollout column (reflects a
        limited-to-wide rollout rather than a single release date)
      - "release_group_url": the link behind "Original Release", which
        leads to a page with the actual single domestic release date
        (see `extract_domestic_date_from_release_group_page` below)
    """
    rollout, release_group_url = None, None

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
        data_cells = rows[1].find_all("td")

        if "rollout" in header_cells:
            rollout_idx = header_cells.index("rollout")
            if len(data_cells) > rollout_idx:
                rollout = data_cells[rollout_idx].get_text(" ", strip=True)

        # First data row's first cell is normally "Release Group" with a
        # link to the Original Release's own page.
        if data_cells:
            link = data_cells[0].find("a")
            if link and link.get("href"):
                href = link["href"]
                release_group_url = BASE_URL + href if href.startswith("/") else href
        break

    return {"rollout": rollout, "release_group_url": release_group_url}


def extract_domestic_date_from_release_group_page(html: str) -> Optional[str]:
    """
    Parses a Box Office Mojo /releasegroup/grXXXXXXXXXX/ page (reached via
    the "Original Release" link) to find the single, clean domestic release
    date. These pages structure the regional breakdown a bit differently
    than title pages - each region is its own table with a colspan header
    row instead of a preceding <h3>:

        <table class="... releases-by-region">
          <tr><th colspan="4">Domestic</th></tr>
          <tr><th>Market</th><th>Release Date</th><th>Opening</th><th>Gross</th></tr>
          <tr><td>Domestic</td><td>Aug 21, 2009</td><td>...</td><td>...</td></tr>
        </table>
    """
    soup = BeautifulSoup(html, "html.parser")

    for th in soup.find_all("th", attrs={"colspan": True}):
        if th.get_text(strip=True).lower() != "domestic":
            continue
        table = th.find_parent("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        header_cells = [c.get_text(strip=True).lower() for c in rows[1].find_all(["th", "td"])]
        if "release date" not in header_cells:
            continue
        date_idx = header_cells.index("release date")

        data_cells = rows[2].find_all("td")
        if len(data_cells) > date_idx:
            value = data_cells[date_idx].get_text(" ", strip=True)
            return value or None
        break

    return None


def fetch_weekend_summary(release_base_url: str) -> dict:
    """
    Fetches <release_base_url>weekend/ and returns:

      - weeks_in_theaters, prev_weekend_gross, prev_weekend_theaters,
        prev_weekend_date: stats for the most recent weekend on record -
        determined by picking the row with the highest week number, not by
        assuming table row order (safer, since BOM's default sort isn't
        guaranteed).
      - widest_release: the highest theater count seen across ALL weekend
        rows for this release. BOM's summary panel sometimes shows a
        "Widest Release" field and sometimes omits it entirely (it seems
        to disappear once a title has enough tracked history), so this is
        computed directly from the weekend table instead, which is always
        present and reliable.

    Returns an empty dict if the page or table can't be found/parsed.
    """
    resp = _get_with_retry(release_base_url + "weekend/")
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return {}

    best = None
    widest_theaters_int, widest_theaters_str = None, None

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 9:
            continue

        theaters_str = cells[4].get_text(strip=True)
        try:
            theaters_int = int(theaters_str.replace(",", ""))
        except ValueError:
            theaters_int = None
        if theaters_int is not None and (widest_theaters_int is None or theaters_int > widest_theaters_int):
            widest_theaters_int = theaters_int
            widest_theaters_str = theaters_str

        try:
            week_num = int(cells[8].get_text(strip=True).replace(",", ""))
        except ValueError:
            continue

        sunday_date = None
        link = cells[0].find("a")
        if link and link.get("href"):
            m = re.search(r"/weekend/(\d{4})W(\d+)/", link["href"])
            if m:
                year, week = int(m.group(1)), int(m.group(2))
                try:
                    sunday_date = datetime.date.fromisocalendar(year, week, 7).strftime("%b %d, %Y")
                except ValueError:
                    pass

        if best is None or week_num > best["weeks_in_theaters"]:
            best = {
                "weeks_in_theaters": week_num,
                "prev_weekend_gross": cells[2].get_text(strip=True),
                "prev_weekend_theaters": cells[4].get_text(strip=True),
                "prev_weekend_date": sunday_date,
            }

    if best is None:
        return {}

    best["widest_release"] = widest_theaters_str
    return best


def fetch_daily_summary(release_base_url: str) -> dict:
    """
    Fetches <release_base_url> itself (the base release page - BOM embeds
    the title's own daily performance table directly there under a
    "Domestic Daily" tab; there is no separate /date/ sub-path for a
    title's own daily data, unlike the /weekend/ sub-path which does
    exist). Returns the most recently recorded day's figures - determined
    by parsing each row's actual encoded date (from its link, e.g.
    /date/2026-07-12/) and taking the max, rather than assuming table row
    order:

      - last_recorded_date
      - last_recorded_gross (that single day's gross, not cumulative)

    Returns an empty dict if the page or table can't be found/parsed.
    """
    resp = _get_with_retry(release_base_url)
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return {}

    best = None
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[0].find("a")
        if not (link and link.get("href")):
            continue
        m = re.search(r"/date/(\d{4}-\d{2}-\d{2})/", link["href"])
        if not m:
            continue
        try:
            date_obj = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        if best is None or date_obj > best["_date_obj"]:
            best = {
                "_date_obj": date_obj,
                "last_recorded_date": date_obj.strftime("%b %d, %Y"),
                "last_recorded_gross": cells[3].get_text(strip=True),
            }

    if best:
        best.pop("_date_obj", None)
    return best or {}


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
        # BOM often renders the year directly attached to the title, e.g.
        # "Gail Daughtry and the Celebrity Sex Pass(2026)" - strip it off.
        title = re.sub(r"\(\d{4}\)\s*$", "", title).strip()

    # --- Release date + Domestic total ---
    domestic_row = _extract_domestic_release_row(soup)
    release_date = domestic_row["release_date"]
    domestic_section_found = domestic_row["domestic_section_found"]

    # Fallback for release date: older / limited-then-wide titles often
    # lack the clean per-region "Domestic" table above, but have a
    # "By Release" table instead, plus a link to a release-group page with
    # a precise single date (the caller follows this - see lookup_title).
    release_group_url = None
    by_release_found = False
    if release_date is None:
        by_release = _extract_by_release_info(soup)
        if by_release["rollout"] or by_release["release_group_url"]:
            by_release_found = True
        release_date = by_release["rollout"]
        release_group_url = by_release["release_group_url"]

    # A domestic release exists if we found either a "Domestic" per-region
    # table OR a "By Release" rollout table (used by older titles). If
    # neither is present, this title was never released in U.S. theaters -
    # some BOM pages only list international regions (e.g. "Asia Pacific",
    # "Europe, Middle East, and Africa") with no "Domestic" section at all.
    has_domestic_release = domestic_section_found or by_release_found

    if has_domestic_release:
        # Domestic total priority:
        #  1. Top "All Releases" summary box - most current, especially for
        #     titles still actively tracking new box office day to day.
        #  2. Per-region "Domestic" table's Gross column - can lag behind #1
        #     for very recent/still-running titles.
        #  3. First "money" span on the page - last-resort fallback.
        domestic_total = _extract_summary_box_domestic_total(soup)
        if domestic_total is None:
            domestic_total = domestic_row["domestic_total"]
        if domestic_total is None:
            money_spans = soup.select("span.money")
            if money_spans:
                domestic_total = money_spans[0].get_text(strip=True)
    else:
        # No domestic release anywhere on the page - default to $0 rather
        # than falling back to an international/worldwide figure, which is
        # what the old logic used to do by mistake.
        domestic_total = "$0"

    # --- Widest Release ---
    # (fallback only - the weekend-table-derived value in lookup_title takes
    # priority since it's more reliable; this is just used when that isn't
    # available, e.g. no release_base_url was found)
    widest_release = _extract_summary_label_value(soup, "Widest Release")
    if widest_release:
        widest_release = re.sub(r"\s*theaters?\s*$", "", widest_release, flags=re.IGNORECASE).strip()

    # --- IMDb tt-code ---
    # Appears reliably in pro.imdb.com links and/or the release-group
    # dropdown value on every page template we've seen (title page,
    # release page, release-group page).
    tt_match = re.search(r"/title/(tt\d{7,9})", html)
    tt_code = tt_match.group(1) if tt_match else None

    return {
        "title": title,
        "tt_code": tt_code,
        "release_date": release_date,
        "domestic_total": domestic_total,
        "widest_release": widest_release,
        "release_group_url": release_group_url,
        "release_base_url": domestic_row["release_base_url"],
    }


def lookup_title(title: str) -> BoxOfficeResult:
    """
    Full pipeline for a single title: search -> fetch -> parse.
    Never raises - errors are captured on the result object so batch runs
    don't die on one bad title.

    Accepts either:
      - A plain title string, e.g. "Oppenheimer" (goes through BOM search,
        which can occasionally mismatch on generic/ambiguous titles)
      - An IMDb tt-code, bare (e.g. "tt15398776") or embedded in a pasted
        BOM URL (e.g. "https://www.boxofficemojo.com/title/tt15398776/...")
        - this goes straight to the title page and skips search entirely,
        which is both faster and more reliable when you already know the
        exact title BOM has on file.
    """
    result = BoxOfficeResult(query=title)
    try:
        tt_match = re.search(r"(tt\d{7,9})", title, re.IGNORECASE)
        bom_title = None
        if tt_match:
            url = f"{BASE_URL}/title/{tt_match.group(1).lower()}/"
        else:
            found = find_release_url(title)
            if not found:
                result.status = "not_found"
                return result
            bom_title, url = found

        result.url = url

        page = _get(url)
        parsed = parse_release_page(page.text)

        result.title = parsed["title"] or bom_title
        # Prefer the tt-code found on the page itself; fall back to the one
        # the user typed in (if they searched by tt-code/URL) as a safety net.
        result.tt_code = parsed["tt_code"] or (tt_match.group(1).lower() if tt_match else None)
        result.release_date = parsed["release_date"]
        result.domestic_total = parsed["domestic_total"]
        result.widest_release = parsed["widest_release"]
        result.status = "ok"

        # If we only got a rollout date *range* (not a single clean date),
        # follow the "Original Release" link to the release-group page,
        # which has the actual single domestic release date.
        if parsed.get("release_group_url"):
            time.sleep(1)  # small politeness pause before the extra fetch
            try:
                rg_page = _get_with_retry(parsed["release_group_url"])
                precise_date = extract_domestic_date_from_release_group_page(rg_page.text)
                if precise_date:
                    result.release_date = precise_date
            except requests.exceptions.RequestException as e:
                # Not fatal - just keep the rollout range we already have,
                # but note it so partial failures aren't silently invisible.
                result.error = f"release-group lookup failed: {e}"
                logger.warning(f"[{title}] release-group lookup failed: {type(e).__name__}: {e}")

        # Weekend + daily performance (mainly relevant for titles still in
        # theaters - both need a release_base_url, which comes from the
        # "Domestic" table on the main page).
        if parsed.get("release_base_url"):
            base = parsed["release_base_url"]

            time.sleep(1.5)
            try:
                weekend = fetch_weekend_summary(base)
                result.weeks_in_theaters = weekend.get("weeks_in_theaters")
                result.prev_weekend_gross = weekend.get("prev_weekend_gross")
                result.prev_weekend_date = weekend.get("prev_weekend_date")
                result.prev_weekend_theaters = weekend.get("prev_weekend_theaters")
                # Prefer the computed max-theater-count value (reliable,
                # always present when weekend data exists) over the
                # summary-panel field (inconsistently present on BOM).
                result.widest_release = weekend.get("widest_release") or result.widest_release
            except requests.exceptions.RequestException as e:
                result.error = f"weekend lookup failed: {e}"
                logger.warning(f"[{title}] weekend lookup failed: {type(e).__name__}: {e}")

            time.sleep(2)
            try:
                daily = fetch_daily_summary(base)
                result.last_recorded_date = daily.get("last_recorded_date")
                result.last_recorded_gross = daily.get("last_recorded_gross")
            except requests.exceptions.RequestException as e:
                result.error = f"daily lookup failed: {e}"
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(f"[{title}] daily lookup failed: {type(e).__name__}: {e} (status={status_code})")

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
