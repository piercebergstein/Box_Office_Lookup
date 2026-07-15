"""
Box Office Mojo Lookup - Streamlit app

Paste a list of movie titles (one per line), hit Run, and get back a table
of Title / Release Date / Domestic Box Office, with a CSV download.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import streamlit as st
import pandas as pd

from scraper import lookup_titles

st.set_page_config(page_title="Box Office Mojo Lookup", page_icon="\U0001F3AC", layout="centered")

st.title("\U0001F3AC Box Office Mojo Lookup")
st.write(
    "Paste a list of movie titles (one per line). This will look each one up on "
    "Box Office Mojo and return the title, release date, and domestic box office total."
)

with st.expander("Notes / limitations", expanded=False):
    st.markdown(
        """
- This does a live web search + fetch for **each** title, and for titles
  still in theaters it now makes a few extra requests (weekend + daily
  performance pages), so a list of 30 titles can take a few minutes.
- If a title comes back as **not found**, try the most standard version of the
  name (e.g. drop "The" or a subtitle) - BOM's search can be picky about exact wording.
- Domestic total reflects whatever BOM currently shows (lifetime-to-date for
  films still in theaters, final for completed runs).
- **Weeks in Theaters**, **Prev Weekend** fields, and **Last Recorded**
  fields are mainly meaningful for titles still actively tracking in
  theaters. For older/completed titles these may come back blank - that's
  expected, not a bug.
- This is for personal / internal analysis. Box Office Mojo is an IMDb/Amazon
  property - don't hammer it with high request volume or redistribute scraped
  data commercially.
        """
    )

titles_input = st.text_area(
    "Titles (one per line)",
    height=200,
    placeholder="Oppenheimer\nBarbie\nDune: Part Two",
)

col1, col2 = st.columns([1, 3])
with col1:
    run = st.button("Run lookup", type="primary")

if run:
    titles = [t for t in titles_input.splitlines() if t.strip()]
    if not titles:
        st.warning("Paste at least one title first.")
    else:
        progress_bar = st.progress(0, text="Starting...")

        def update_progress(i, total, title):
            progress_bar.progress((i) / total, text=f"Looking up: {title} ({i+1}/{total})")

        results = lookup_titles(titles, progress_callback=update_progress)
        progress_bar.progress(1.0, text="Done")

        rows = []
        for r in results:
            rows.append(
                {
                    "Title": r.title or r.query,
                    "Release Date": r.release_date or "",
                    "Domestic Box Office": r.domestic_total or "",
                    "Widest Release": r.widest_release or "",
                    "Weeks in Theaters": r.weeks_in_theaters or "",
                    "Prev Weekend Gross": r.prev_weekend_gross or "",
                    "Prev Weekend Date": r.prev_weekend_date or "",
                    "Prev Weekend Theaters": r.prev_weekend_theaters or "",
                    "Last Recorded Date": r.last_recorded_date or "",
                    "Last Recorded Gross": r.last_recorded_gross or "",
                    "BOM URL": r.url or "",
                }
            )

        df = pd.DataFrame(rows)
        st.success(f"Done - {sum(1 for r in results if r.status == 'ok')} of {len(results)} found.")
        st.dataframe(df, use_container_width=True)

        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download as CSV",
            data=csv,
            file_name="box_office_results.csv",
            mime="text/csv",
        )
