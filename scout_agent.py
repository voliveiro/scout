"""
scout_agent.py — The core AI loop for Scout.

Two responsibilities:
  1. run()     — scrape each source URL, extract items, persist to DB
  2. analyse() — run analysis agents over a completed run's items
"""

import sqlite3
import json
from datetime import datetime
import anthropic


class ScoutAgent:

    SCRAPE_SYSTEM_RESEARCH = """You are Scout, a research intelligence agent working for a governance research team.
You are given a URL for a policy research institution's publications page.
Use web_search to visit the page and extract up to 6 recent research items.

Return ONLY a JSON array. No markdown, no explanation, no preamble.
Format:
[
  {
    "title": "Full title of the paper or report",
    "author": "Author name(s) or empty string if not found",
    "summary": "One or two sentences describing what this research is about and why it matters."
  }
]

If you cannot find any items, return [].
"""

    SCRAPE_SYSTEM_EVENTS = """You are Scout, a research intelligence agent working for a governance research team.
You are given a URL for a policy institution's events page.
Use web_search to visit the page and extract up to 6 upcoming or recent events.

Return ONLY a JSON array. No markdown, no explanation, no preamble.
Format:
[
  {
    "title": "Full title or name of the event",
    "speaker": "Speaker or panellist name(s), or empty string if not found",
    "summary": "One or two sentences describing what the event is about and why it is significant."
  }
]

If you cannot find any items, return [].
"""

    ANALYSIS_SYSTEM = """You are Scout's analysis layer. You have been given a structured dataset of recent research publications and events collected from major policy institutions (Harvard Kennedy School, Oxford Blavatnik, Lee Kuan Yew School, Brookings, Chatham House, RAND).

Your job is to write a concise, useful analytical summary for a team of governance researchers and public servants. Write in plain, direct prose — not bullet points. No preamble. Be specific, naming actual titles and institutions where relevant.
"""

    def __init__(self, db_path: str, api_key: str):
        self.db_path = db_path
        self.client = anthropic.Anthropic(api_key=api_key)

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Scrape loop ───────────────────────────────────────────────────────────

    def run(self):
        conn = self.get_db()
        sources = conn.execute("SELECT * FROM sources ORDER BY entity, type").fetchall()

        # Create run record
        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO runs (started_at, status, total_urls) VALUES (?,?,?)",
            (now, "running", len(sources))
        )
        run_id = cur.lastrowid
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
                items = self._scrape(source["url"], source["type"])
                saved = 0
                for item in items:
                    conn.execute(
                        """INSERT INTO items
                           (run_id, entity, type, url, title, author, speaker, summary, first_seen)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            source["entity"],
                            source["type"],
                            source["url"],
                            item.get("title", ""),
                            item.get("author", ""),
                            item.get("speaker", ""),
                            item.get("summary", ""),
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
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, total_items=? WHERE id=?",
            (datetime.now().isoformat(), "done", total_items, run_id)
        )
        conn.commit()
        conn.close()

        yield {
            "type": "run_complete",
            "run_id": run_id,
            "total_items": total_items,
            "errors": errors
        }

    def _scrape(self, url: str, source_type: str) -> list:
        system = (
            self.SCRAPE_SYSTEM_RESEARCH
            if source_type == "Research"
            else self.SCRAPE_SYSTEM_EVENTS
        )

        response = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"Visit this page and extract the listings: {url}"
            }]
        )

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
        items = conn.execute(
            "SELECT * FROM items WHERE run_id = ?", (run_id,)
        ).fetchall()

        if not items:
            yield {"type": "error", "message": "No items found for this run."}
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
        conn.execute(
            "INSERT INTO analyses (run_id, type, content, created_at) VALUES (?,?,?,?)",
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
        conn.execute(
            "INSERT INTO analyses (run_id, type, content, created_at) VALUES (?,?,?,?)",
            (run_id, "Executive Summary", digest, now)
        )
        conn.commit()
        yield {"type": "analysis_done", "label": "Summary digest", "content": digest}

        # 3. Diff commentary (if prior run exists)
        prior = conn.execute(
            "SELECT id FROM runs WHERE status='done' AND id != ? ORDER BY finished_at DESC LIMIT 1",
            (run_id,)
        ).fetchone()

        if prior:
            yield {"type": "analysis_start", "label": "What's new since last run"}
            prior_titles = set(
                r["title"] for r in conn.execute(
                    "SELECT title FROM items WHERE run_id = ?", (prior["id"],)
                ).fetchall() if r["title"]
            )
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
            conn.execute(
                "INSERT INTO analyses (run_id, type, content, created_at) VALUES (?,?,?,?)",
                (run_id, "New Since Last Run", diff, now)
            )
            conn.commit()
            yield {"type": "analysis_done", "label": "What's new since last run", "content": diff}

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
