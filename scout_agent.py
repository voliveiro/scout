"""
scout_agent.py — The core AI loop for Scout.

Two responsibilities:
  1. run()     — scrape each source URL, extract items, persist to DB
  2. analyse() — run analysis agents over a completed run's items
"""

import json
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import anthropic


class ScoutAgent:

    SCRAPE_SYSTEM_RESEARCH = """You are Scout, a research intelligence agent working for a governance research team.
You are given a URL for a policy research institution's publications page.
Today's date is {today}.

Your task:
1. Use web_search to visit the URL and extract research published in the PAST 30 DAYS only.
2. If the page is paginated (e.g. ?page=2, ?page=3), follow subsequent pages until you either reach items older than 30 days or run out of pages. Do not go beyond page 5.
3. Ignore research older than 30 days.

Return ONLY a JSON array. No markdown, no explanation, no preamble.
Format:
[
  {
    "title": "Full title of the paper or report",
    "author": "Author name(s) or empty string if not found",
    "date": "Publication date, e.g. 12 March 2026",
    "summary": "One or two sentences describing what this research is about and why it matters."
  }
]

If you cannot find any items within the past 30 days, return [].
"""

    SCRAPE_SYSTEM_EVENTS = """You are Scout, a research intelligence agent working for a governance research team.
You are given a URL for a policy institution's events page.
Today's date is {today}.

Your task:
1. Use web_search to visit the URL and extract upcoming or recent events from the PAST 30 DAYS only.
2. If the page is paginated (e.g. ?page=2, ?page=3), follow subsequent pages until you either reach events older than 30 days or run out of pages. Do not go beyond page 5.
3. Ignore events older than 30 days.

Return ONLY a JSON array. No markdown, no explanation, no preamble.
Format:
[
  {
    "title": "Full title or name of the event",
    "speaker": "Speaker or panellist name(s), or empty string if not found",
    "date": "Date of the event, e.g. 12 March 2026",
    "summary": "One or two sentences describing what the event is about and why it is significant."
  }
]

If you cannot find any items within the past 30 days, return [].
"""

    ANALYSIS_SYSTEM = """You are Scout's analysis layer. You have been given a structured dataset of recent research publications and events collected from major policy institutions (Harvard Kennedy School, Oxford Blavatnik, Lee Kuan Yew School, Brookings, Chatham House, RAND).

Your job is to write a concise, useful analytical summary for a team of governance researchers and public servants. Write in plain, direct prose — not bullet points. No preamble. Be specific, naming actual titles and institutions where relevant.
"""

    def __init__(self, db_url: str, api_key: str):
        self.db_url = db_url
        self.client = anthropic.Anthropic(api_key=api_key, timeout=60.0)

    def get_db(self):
        conn = psycopg2.connect(self.db_url, sslmode='disable')
        return conn

    # ── Scrape loop ───────────────────────────────────────────────────────────

    def run(self):
        conn = self.get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM sources ORDER BY entity, type")
        sources = cur.fetchall()

        # Create run record
        now = datetime.now().isoformat()
        cur.execute(
            "INSERT INTO runs (started_at, status, total_urls) VALUES (%s,%s,%s) RETURNING id",
            (now, "running", len(sources))
        )
        run_id = cur.fetchone()["id"]
        conn.commit()

        yield {"type": "run_start", "run_id": run_id, "total": len(sources)}

        total_items = 0
        errors = 0

        for i, source in enumerate(sources):
            yield {
                "type": "url_start",
                "index": i,
                "entity": source["entity"],
                "source_type": source["type"],
                "url": source["url"]
            }

            try:
                yield {
                    "type": "log",
                    "message": f"Calling API for {source['url']}…"
                }
                items = self._scrape(source["url"], source["type"])
                yield {
                    "type": "log",
                    "message": f"API returned {len(items)} items."
                }
                saved = 0
                for item in items:
                    cur.execute(
                        """INSERT INTO items
                           (run_id, entity, type, url, title, author, speaker, summary, date, first_seen)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            run_id,
                            source["entity"],
                            source["type"],
                            source["url"],
                            item.get("title", ""),
                            item.get("author", ""),
                            item.get("speaker", ""),
                            item.get("summary", ""),
                            item.get("date", ""),
                            now
                        )
                    )
                    saved += 1
                conn.commit()
                total_items += saved

                yield {
                    "type": "url_done",
                    "index": i,
                    "entity": source["entity"],
                    "source_type": source["type"],
                    "count": saved,
                    "items": items
                }

            except Exception as e:
                errors += 1
                yield {
                    "type": "url_error",
                    "index": i,
                    "entity": source["entity"],
                    "url": source["url"],
                    "error": str(e)
                }

        # Finalise run
        cur.execute(
            "UPDATE runs SET finished_at=%s, status=%s, total_items=%s WHERE id=%s",
            (datetime.now().isoformat(), "done", total_items, run_id)
        )
        conn.commit()
        cur.close()
        conn.close()

        yield {
            "type": "run_complete",
            "run_id": run_id,
            "total_items": total_items,
            "errors": errors
        }

        # Auto-run analysis so diff commentary is always available
        yield {"type": "log", "message": "Starting automatic analysis…"}
        yield from self.analyse(run_id)

    def _scrape(self, url: str, source_type: str) -> list:
        print(f"SCRAPE START: {url}", flush=True)
        today = datetime.now().strftime("%d %B %Y")
        system = (
            self.SCRAPE_SYSTEM_RESEARCH
            if source_type == "Research"
            else self.SCRAPE_SYSTEM_EVENTS
        ).replace("{today}", today)

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            system=system,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"Visit this page and extract the listings: {url}"
            }]
        )

        print(f"SCRAPE API DONE: {url}", flush=True)
        raw = ""
        for block in response.content:
            if block.type == "text":
                raw += block.text

        clean = raw.replace("```json", "").replace("```", "").strip()
        bracket = clean.find("[")
        if bracket == -1:
            return []
        return json.loads(clean[bracket:])
    

    # ── Analysis loop ─────────────────────────────────────────────────────────

    def analyse(self, run_id: int):
        conn = self.get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM items WHERE run_id = %s", (run_id,))
        items = cur.fetchall()

        if not items:
            yield {"type": "error", "message": "No items found for this run."}
            cur.close()
            conn.close()
            return

        corpus = self._build_corpus(items)
        now = datetime.now().isoformat()

        # 1. Thematic clustering
        yield {"type": "analysis_start", "label": "Thematic clustering"}
        themes = self._run_analysis(
            corpus,
            """Identify 4-6 major themes that cut across the research and events in this dataset.
For each theme, name it, describe it in 2-3 sentences, and cite 2-3 specific items (by title and institution) that exemplify it.
Focus on cross-institutional patterns — what are multiple institutions paying attention to simultaneously?"""
        )
        cur.execute(
            "INSERT INTO analyses (run_id, type, content, created_at) VALUES (%s,%s,%s,%s)",
            (run_id, "Thematic Clusters", themes, now)
        )
        conn.commit()
        yield {"type": "analysis_done", "label": "Thematic clustering", "content": themes}

        # 2. Summary digest
        yield {"type": "analysis_start", "label": "Summary digest"}
        digest = self._run_analysis(
            corpus,
            """Write a 3-4 paragraph executive summary of this week's research and events landscape across these institutions.
Address: what is the dominant preoccupation this week, what is surprising or notable, and what gaps or silences are worth flagging.
Write for a senior governance researcher who will share this with their team."""
        )
        cur.execute(
            "INSERT INTO analyses (run_id, type, content, created_at) VALUES (%s,%s,%s,%s)",
            (run_id, "Executive Summary", digest, now)
        )
        conn.commit()
        yield {"type": "analysis_done", "label": "Summary digest", "content": digest}

        # 3. Diff commentary (if prior run exists)
        cur.execute(
            "SELECT id FROM runs WHERE status='done' AND id != %s ORDER BY finished_at DESC LIMIT 1",
            (run_id,)
        )
        prior = cur.fetchone()

        if prior:
            yield {"type": "analysis_start", "label": "What's new since last run"}
            cur.execute("SELECT title FROM items WHERE run_id = %s", (prior["id"],))
            prior_titles = set(r["title"] for r in cur.fetchall() if r["title"])
            new_items = [dict(r) for r in items if r["title"] and r["title"] not in prior_titles]
            if new_items:
                new_corpus = self._build_corpus(new_items)
                diff = self._run_analysis(
                    new_corpus,
                    f"""These {len(new_items)} items are new since the previous Scout run.
Write a concise paragraph (4-6 sentences) summarising what has appeared since last time.
What directions are emerging? What institutions are most active? What should the team pay closest attention to?"""
                )
            else:
                diff = "No new items detected since the previous run."
            cur.execute(
                "INSERT INTO analyses (run_id, type, content, created_at) VALUES (%s,%s,%s,%s)",
                (run_id, "New Since Last Run", diff, now)
            )
            conn.commit()
            yield {"type": "analysis_done", "label": "What's new since last run", "content": diff}

        cur.close()
        conn.close()

    def _run_analysis(self, corpus: str, instruction: str) -> str:
        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            system=self.ANALYSIS_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"{instruction}\n\nDATA:\n{corpus}"
            }]
        )
        return response.content[0].text

    def _build_corpus(self, items) -> str:
        lines = []
        for item in items:
            item = dict(item)
            lines.append(
                f"[{item['entity']} / {item['type']}] "
                f"{item['title'] or '(no title)'} "
                f"| {item.get('author') or item.get('speaker') or ''} "
                f"| {item['summary'] or ''}"
            )
        return "\n".join(lines)
