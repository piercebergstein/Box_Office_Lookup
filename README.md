# Box Office Mojo Lookup

Paste a list of movie titles, get back Title / Release Date / Domestic Box Office
for each, plus a CSV download. Built as a small Streamlit app on top of a
standalone Python scraper module.

## Why you're testing this yourself first

This was built and unit-tested against Box Office Mojo's known page structure
(verified with a synthetic HTML sample matching their real layout), but it
could **not** be run against the live boxofficemojo.com site from the
environment it was built in - that sandbox's network access is locked to a
handful of package registries, and separately, automated fetches to BOM are
getting blocked at the tool level (likely bot-detection on an Amazon/IMDb
property, even though their robots.txt technically allows crawling).

Practically: the scraping logic is real and grounded in BOM's actual markup,
but you're the first one to run it against the live site. If a couple of
fields come back empty, it's a quick fix - see "If something breaks" below.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

This opens a local web page in your browser - paste titles, hit "Run lookup."

## Running just the scraper (no UI)

```bash
python scraper.py "Oppenheimer" "Barbie" "Dune: Part Two"
```

## Deploying so you can access it from anywhere (not just your laptop)

Easiest free option: push this folder to a GitHub repo, then deploy on
[Streamlit Community Cloud](https://streamlit.io/cloud) (free tier, point it
at your repo + `app.py`). You'll get a shareable URL.

Alternative: paste this code into a new Replit (Python template), install the
same requirements, and use Replit's "Run" + web preview.

## If something breaks

Box Office Mojo's HTML structure changes occasionally. If titles come back
"not_found" when you know they exist, or a field (release date / domestic
total) is empty:

1. Run this for one title to save the raw page:
   ```python
   from scraper import find_release_url, _get
   title, url = find_release_url("Oppenheimer")
   html = _get(url).text
   open("sample_page.html", "w").write(html)
   ```
2. Open `sample_page.html` in a browser, right-click the field that's missing
   (e.g. the domestic total number) -> **Inspect**.
3. Note the class name wrapping it, and update the matching selector in
   `scraper.py` (`parse_release_page` / `_extract_summary_field`).

Send me what you find (e.g. a snippet of the new HTML around that field) and
I can update the selectors directly.

## A note on scraping etiquette

- There's a 1.5 second delay built in between requests - don't remove it for
  large batches, it's what keeps this from looking like an attack on their
  servers.
- This is scoped for personal/internal analysis (e.g. pulling comps for your
  own tracking), not for redistributing BOM's data or running at high volume.
  Box Office Mojo is an IMDb/Amazon property with its own terms of use worth
  a skim if you plan to scale this up.

## Files

- `scraper.py` - core logic: search BOM, fetch a title's page, parse title /
  release date / domestic total. Also runnable standalone.
- `app.py` - Streamlit UI: paste box, results table, CSV export.
- `requirements.txt` - dependencies.
