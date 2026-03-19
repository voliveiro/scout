"""
Scout — Research Intelligence Agent
Flask backend, port 5002
Sibling to Nuncio (port 5001)
"""

import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import threading
from datetime import datetime
from flask import Flask, jsonify, request, render_template, Response, stream_with_context, make_response
from dotenv import load_dotenv
from scout_agent import ScoutAgent

load_dotenv()

app = Flask(__name__)

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"), sslmode='disable')
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id          SERIAL PRIMARY KEY,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            status      TEXT DEFAULT 'running',
            total_urls  INTEGER DEFAULT 0,
            total_items INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id          SERIAL PRIMARY KEY,
            run_id      INTEGER NOT NULL,
            entity      TEXT NOT NULL,
            type        TEXT NOT NULL,
            url         TEXT NOT NULL,
            title       TEXT,
            author      TEXT,
            speaker     TEXT,
            summary     TEXT,
            date        TEXT,
            first_seen  TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id          SERIAL PRIMARY KEY,
            run_id      INTEGER NOT NULL,
            type        TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id      SERIAL PRIMARY KEY,
            entity  TEXT NOT NULL,
            type    TEXT NOT NULL,
            url     TEXT NOT NULL UNIQUE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def seed_sources():
    """Load sources from sources.csv if table is empty."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sources")
    count = cur.fetchone()[0]
    if count == 0:
        import csv
        csv_path = os.path.join(os.path.dirname(__file__), "sources.csv")
        if os.path.exists(csv_path):
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cur.execute(
                        "INSERT INTO sources (entity, type, url) VALUES (%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                        (row["Entity"], row["Type"], row["URL"])
                    )
        conn.commit()
    cur.close()
    conn.close()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.route("/api/sources", methods=["GET"])
def get_sources():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM sources ORDER BY entity, type")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sources", methods=["POST"])
def add_source():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO sources (entity, type, url) VALUES (%s,%s,%s) ON CONFLICT (url) DO NOTHING",
            (data["entity"], data["type"], data["url"])
        )
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 409
    finally:
        cur.close()
        conn.close()

@app.route("/api/sources/<int:source_id>", methods=["DELETE"])
def delete_source(source_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE id = %s", (source_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/runs", methods=["GET"])
def get_runs():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 20")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/runs/<int:run_id>/items", methods=["GET"])
def get_run_items(run_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM items WHERE run_id = %s ORDER BY entity, type", (run_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/runs/latest/items", methods=["GET"])
def get_latest_items():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM runs WHERE status = 'done' ORDER BY finished_at DESC LIMIT 1")
    run = cur.fetchone()
    if not run:
        cur.close()
        conn.close()
        return jsonify([])
    cur.execute("SELECT * FROM items WHERE run_id = %s ORDER BY entity, type", (run["id"],))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/runs/<int:run_id>/analyses", methods=["GET"])
def get_analyses(run_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM analyses WHERE run_id = %s ORDER BY created_at", (run_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/runs/latest/diff-analysis", methods=["GET"])
def get_latest_diff_analysis():
    """Return the 'New Since Last Run' analysis from the most recent completed run."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM runs WHERE status = 'done' ORDER BY finished_at DESC LIMIT 1")
    run = cur.fetchone()
    if not run:
        cur.close()
        conn.close()
        return jsonify(None)
    cur.execute(
        "SELECT content FROM analyses WHERE run_id = %s AND type = 'New Since Last Run' ORDER BY created_at DESC LIMIT 1",
        (run["id"],)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(row["content"] if row else None)

@app.route("/api/diff", methods=["GET"])
def get_diff():
    """Return items that are new since the previous run."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM runs WHERE status = 'done' ORDER BY finished_at DESC LIMIT 2")
    runs = cur.fetchall()
    if len(runs) < 2:
        cur.close()
        conn.close()
        return jsonify({"message": "Need at least two completed runs to diff.", "items": []})
    latest_id, prev_id = runs[0]["id"], runs[1]["id"]
    cur.execute("SELECT title FROM items WHERE run_id = %s", (prev_id,))
    prev_titles = set(r["title"] for r in cur.fetchall() if r["title"])
    cur.execute("SELECT * FROM items WHERE run_id = %s", (latest_id,))
    new_items = [dict(r) for r in cur.fetchall() if r["title"] and r["title"] not in prev_titles]
    cur.close()
    conn.close()
    return jsonify({"new_count": len(new_items), "items": new_items})

@app.route("/api/digest/<int:run_id>", methods=["GET"])
def get_digest(run_id):
    """Return a plain-text shareable digest for a run."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
    run = cur.fetchone()
    cur.execute("SELECT * FROM items WHERE run_id = %s ORDER BY entity, type", (run_id,))
    items = cur.fetchall()
    cur.execute("SELECT * FROM analyses WHERE run_id = %s", (run_id,))
    analyses = cur.fetchall()
    cur.close()
    conn.close()

    if not run:
        return "Run not found", 404

    lines = []
    lines.append("SCOUT RESEARCH DIGEST")
    lines.append("=" * 60)
    lines.append(f"Run date: {run['started_at'][:10]}")
    lines.append(f"Items collected: {run['total_items']}")
    lines.append("")

    for a in analyses:
        lines.append(f"── {a['type'].upper()} ──")
        lines.append(a["content"])
        lines.append("")

    entities = list(dict.fromkeys(r["entity"] for r in items))
    for entity in entities:
        entity_items = [r for r in items if r["entity"] == entity]
        lines.append("=" * 60)
        lines.append(entity.upper())
        lines.append("=" * 60)
        for item in entity_items:
            lines.append(f"\n[{item['type']}] {item['title'] or '(Untitled)'}")
            if item["author"]:
                lines.append(f"  Author: {item['author']}")
            if item["speaker"]:
                lines.append(f"  Speaker: {item['speaker']}")
            if item["summary"]:
                lines.append(f"  {item['summary']}")
        lines.append("")

    run_date = datetime.fromisoformat(run["started_at"]).strftime("%Y%m%d-%H%M")

    return Response(
        "\n".join(lines),
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename=scout_digest_{run_date}.txt"}
    )

# ── Run endpoint with SSE streaming ──────────────────────────────────────────

_run_lock = threading.Lock()
_run_active = False

@app.route("/api/stop", methods=["POST"])
def stop_run():
    global _run_active
    _run_active = False
    return jsonify({"status": "ok"})

@app.route("/api/run", methods=["POST"])
def start_run():
    global _run_active
    if _run_active:
        return jsonify({"error": "A run is already in progress."}), 409

    def generate():
        global _run_active
        _run_active = True
        try:
            agent = ScoutAgent(os.getenv("DATABASE_URL"), os.getenv("ANTHROPIC_API_KEY"))
            for event in agent.run():
                if not _run_active:
                    yield f"data: {json.dumps({'type': 'stopped'})}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            _run_active = False
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

@app.route("/api/analyse/<int:run_id>", methods=["POST"])
def analyse_run(run_id):
    """Run analysis agents over a completed run's items."""
    def generate():
        agent = ScoutAgent(os.getenv("DATABASE_URL"), os.getenv("ANTHROPIC_API_KEY"))
        for event in agent.analyse(run_id):
            yield f"data: {json.dumps(event)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# ── Main ──────────────────────────────────────────────────────────────────────

try:
    init_db()
    seed_sources()
except Exception as e:
    print(f"STARTUP ERROR: {e}", flush=True)
    raise

if __name__ == "__main__":
    print("Scout running on http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=True)