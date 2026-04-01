# Scout — Research Intelligence Agent

Scout is a prototype AI agent that monitors policy research institutions, extracts recent publications and events, and runs analysis across the collected corpus.


---

## What Scout does

Scout visits a configured list of policy institution websites and extracts:

- **Research**: title, author, and a one- to two-sentence summary of each recent publication
- **Events**: title, speaker, and a one- to two-sentence description of each upcoming or recent event

After collecting, Scout runs three analysis passes over the corpus:

- **Thematic clustering** — what themes are cutting across multiple institutions this week?
- **Executive summary** — a short digest suitable for sharing with the team
- **What's new** — a diff against the previous run, highlighting items that weren't there before

Results are stored in a local SQLite database, so each run builds on the last. You can download a plain-text digest from any run to share with colleagues who don't have access to the tool.

---

## Setup

**Requirements:** Python 3.10+, an Anthropic API key with credits

```bash
# 1. Clone the repo
git clone https://github.com/your-username/scout.git
cd scout

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
nano .env  # paste your ANTHROPIC_API_KEY

# 5. Run
python3 app.py
```

Scout will be available at `http://localhost:5002`.

---

## Using Scout

1. **Run Scout** — click the button and Scout will work through all configured sources, streaming results to the screen as it goes. Expect 3–5 minutes for a full run.
2. **Analyse** — once a run is complete, click Analyse. Scout will generate thematic clusters, an executive summary, and (from the second run onwards) a diff against the previous run.
3. **Download Digest** — produces a plain-text file you can email or paste into a document.
4. **Past Runs** — all runs are saved. You can revisit any previous run from the History tab.

---

## Adding or removing sources

The easiest way is through the **Sources** tab in the UI — add an institution name, select Research or Events, and paste the URL. Changes take effect on the next run.

To seed a fresh installation with a different set of institutions, edit `sources.csv` before the first run. The format is:

```
Entity,Type,URL
Harvard Kennedy School,Research,https://www.hks.harvard.edu/research-insights/publications
```

---

## Project structure

```
scout/
├── app.py              # Flask app — routes, DB, streaming
├── scout_agent.py      # AI agent loop (scrape + analyse)
├── sources.csv         # Seed data, loaded once on first run
├── requirements.txt
├── .env.example        # Copy to .env and add your API key
├── .gitignore
├── data/
│   └── scout.db        # SQLite database (auto-created, not committed)
└── templates/
    └── index.html      # Frontend
```

---

## Extending Scout

**New institutions:** Add them via the Sources tab or `sources.csv`. Any URL that lists publications or events should work — Scout uses Claude with web search, so it reads the page rather than scraping HTML directly.

**New analysis types:** Add a new method to the `analyse()` loop in `scout_agent.py`, following the pattern of the existing three. Each analysis is a prompt over the full corpus, streamed back to the frontend.

**Scheduling:** Scout doesn't run on a schedule by default. To run it weekly, add a cron job:

```bash
# Run Scout every Monday at 8am
0 8 * * 1 cd /path/to/scout && venv/bin/python3 -c "
from scout_agent import ScoutAgent
import os
from dotenv import load_dotenv
load_dotenv()
agent = ScoutAgent('data/scout.db', os.getenv('ANTHROPIC_API_KEY'))
for _ in agent.run(): pass
"
```

---

## A note on costs

Scout uses the Anthropic API with web search enabled. Each full run makes approximately 19–22 API calls (one per source URL, plus three analysis passes).

**Scraping vs. analysis models**
Scraping (one call per source URL) uses **Claude Haiku**, which is fast and well-suited to structured extraction. Analysis (Executive Summary, Thematic Clusters, What's New) uses **Claude Sonnet**, where output quality matters more. This split keeps a full run well under $0.10 USD in most cases — roughly 5–10× cheaper than running everything on Sonnet.

**24-hour cooldown**
Scout enforces a 24-hour gap between runs. This prevents accidental repeated runs from accumulating cost, particularly useful when sharing the tool with a team. The cooldown is enforced server-side; the UI shows how long until the next run is available.

API credits are managed separately from a Claude.ai Pro subscription at console.anthropic.com. Setting a monthly spending limit there is a good backstop.

---

## Coverage vs. performance trade-offs

Scout is designed for speed and reliability over exhaustive coverage. A few deliberate choices shape this:

**Pagination limit — 3 pages per source**
Scout follows up to 3 pages of results per source URL. This keeps each scrape to a manageable length (typically 30–60 seconds) and reduces API cost. A higher page limit would surface more historical content but significantly slows each run and increases cost. If you need deeper coverage from a specific source, consider adding it twice with different entry URLs (e.g. page 1 and page 4).

**Per-source timeout — 2 minutes**
If a source takes longer than 2 minutes to respond — due to a slow server, an unusually large page, or a network issue — Scout skips it and marks it as unresponsive in the run log. The source will be retried on the next run. This prevents one problematic URL from stalling an entire run.

**Time window**
Research is limited to the past 30 days. Events cover a −30 to +90 day window (recent past plus upcoming). Courses cover anything running or starting within the next 6 months. Items outside these windows are ignored even if the page lists them.

---

## Status

This is a prototype. It works, but expect rough edges — error handling is minimal, there's no authentication on the web interface, and the analysis quality depends on how well Claude can read each institution's page structure. Contributions and issues welcome.

---

## Built with

- [Flask](https://flask.palletsprojects.com/) — backend and SSE streaming
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude with web search
- [SQLite](https://www.sqlite.org/) — local persistence
- [Merriweather](https://fonts.google.com/specimen/Merriweather) — because it deserved a decent font
