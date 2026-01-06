import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, abort, render_template

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "data.db")

# Trial rules
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))          # total trial window
MAX_VIEWS = int(os.getenv("MAX_VIEWS", "3"))            # how many 24h windows max
HTML_VALID_HOURS = int(os.getenv("HTML_VALID_HOURS", "24"))

# Security: only YOU can push data
API_KEY = os.getenv("API_KEY", "")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            trial_start TEXT NOT NULL,
            views_used INTEGER NOT NULL DEFAULT 0,
            window_expires_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS payloads (
            client_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


@app.before_request
def _ensure_db():
    init_db()


@app.get("/health")
def health():
    return {"ok": True}


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s)


def trial_active(trial_start_iso: str) -> bool:
    start = datetime.fromisoformat(trial_start_iso)
    return now_utc() <= (start + timedelta(days=TRIAL_DAYS))


def ensure_valid_window(client_row) -> tuple[bool, str]:
    """
    Returns: (allowed, message)
    - Ensures the client has an active 24h access window.
    - If window expired, consumes 1 view and creates a new 24h window (if views remain).
    """
    if not trial_active(client_row["trial_start"]):
        return False, "Trial ended"

    window_exp = parse_dt(client_row["window_expires_at"])
    n = now_utc()

    # If first time or expired: open a new 24h window (consume one view)
    if (window_exp is None) or (n > window_exp):
        if client_row["views_used"] >= MAX_VIEWS:
            return False, "Trial limit reached"

        new_exp = n + timedelta(hours=HTML_VALID_HOURS)

        con = db()
        con.execute(
            "UPDATE clients SET views_used = views_used + 1, window_expires_at = ? WHERE client_id = ?",
            (new_exp.isoformat(), client_row["client_id"])
        )
        con.commit()
        con.close()

    return True, "OK"


@app.post("/push-data")
def push_data():
    # Only you can call this
    if not API_KEY or request.headers.get("X-API-KEY") != API_KEY:
        abort(403)

    payload = request.get_json(force=True)

    client_id = (payload.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"error": "client_id is required"}), 400

    updated_at = now_utc().isoformat()

    con = db()

    # Create trial record if new client
    existing = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO clients (client_id, trial_start, views_used, window_expires_at) VALUES (?,?,0,NULL)",
            (client_id, updated_at)
        )

    con.execute("""
        INSERT INTO payloads (client_id, payload, updated_at)
        VALUES (?,?,?)
        ON CONFLICT(client_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
    """, (client_id, json.dumps(payload, ensure_ascii=False), updated_at))

    con.commit()
    con.close()

    return jsonify({
        "ok": True,
        "public_path": f"/report/{client_id}"
    })


@app.get("/report/<client_id>")
def report(client_id):
    con = db()
    client = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    payload_row = con.execute("SELECT payload FROM payloads WHERE client_id = ?", (client_id,)).fetchone()
    con.close()

    if not client or not payload_row:
        return "No data available yet. Please contact provider.", 404

    allowed, msg = ensure_valid_window(client)
    if not allowed:
        return f"⛔ Access refused: {msg}", 403

    # Re-read to get updated window & views
    con = db()
    client2 = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    con.close()

    window_exp = client2["window_expires_at"]
    views_used = client2["views_used"]

    # Render your SAME UI template; it will fetch JSON from /api/data/<client_id>
    return render_template(
        "dashboard.html",
        client_id=client_id,
        window_expires_at=window_exp,
        views_used=views_used,
        max_views=MAX_VIEWS
    )


@app.get("/api/data/<client_id>")
def api_data(client_id):
    con = db()
    client = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    payload_row = con.execute("SELECT payload FROM payloads WHERE client_id = ?", (client_id,)).fetchone()
    con.close()

    if not client or not payload_row:
        abort(404)

    # Must be inside current 24h window
    if not trial_active(client["trial_start"]):
        abort(403)

    window_exp = parse_dt(client["window_expires_at"])
    if window_exp is None or now_utc() > window_exp:
        abort(403)

    payload = json.loads(payload_row["payload"])
    # You can add watermark info here
    payload["_trial"] = {
        "valid_until": client["window_expires_at"],
        "views_used": client["views_used"],
        "max_views": MAX_VIEWS
    }
    return jsonify(payload)
