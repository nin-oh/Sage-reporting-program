import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, abort, render_template

# ==========================================================
#  Flask app for Render (B1)
#  - POST /push-data   (your laptop pushes JSON with X-API-KEY)
#  - GET  /report/<id> (client opens UI)
#  - GET  /api/report/<id> (UI fetches JSON)
#  - GET  /health
# ==========================================================

app = Flask(__name__)

# ---- Env vars (set in Render dashboard) ----
API_KEY = os.getenv("API_KEY", "")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))                 # total trial duration from first push/open
MAX_VIEWS = int(os.getenv("MAX_VIEWS", "3"))                   # number of 24h windows
HTML_VALID_HOURS = int(os.getenv("HTML_VALID_HOURS", "24"))    # each window duration

DB_PATH = os.getenv("DB_PATH", "data.db")


# -------------------- DB helpers --------------------
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


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s)


def require_api_key():
    if not API_KEY:
        abort(500, description="API_KEY not configured on server")
    if request.headers.get("X-API-KEY") != API_KEY:
        abort(403)


def trial_is_active(trial_start_iso: str) -> bool:
    start = datetime.fromisoformat(trial_start_iso)
    return now_utc() <= (start + timedelta(days=TRIAL_DAYS))


def ensure_access_window(client_row) -> tuple[bool, str]:
    """
    Ensures a valid access window exists.
    - If trial ended => deny
    - If window missing/expired => consume a view and open a new 24h window (if views remain)
    """
    if not trial_is_active(client_row["trial_start"]):
        return False, "Trial ended"

    n = now_utc()
    window_exp = parse_dt(client_row["window_expires_at"])

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


# -------------------- Routes --------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    return {
        "service": "sage-reporting-program",
        "ok": True,
        "endpoints": {
            "health": "/health",
            "push_data": "POST /push-data (X-API-KEY)",
            "report_page": "/report/<client_id>",
            "report_api": "/api/report/<client_id>",
        }
    }


@app.post("/push-data")
def push_data():
    """
    Your local program calls this to upload the computed dashboard data.
    Header: X-API-KEY: <API_KEY>
    JSON payload example:
    {
      "client_id": "RELAISMEDICAL",
      "year": 2025,
      "data": { "CA": {"value":..., "category":"CPC", "type":"Montant", "unit":"MAD"}, ... }
    }
    """
    require_api_key()

    payload = request.get_json(force=True) or {}
    client_id = (payload.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"ok": False, "error": "client_id is required"}), 400

    updated_at = now_utc().isoformat()

    # Store entire payload (so we can return year + meta later)
    con = db()

    client = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    if not client:
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

    return jsonify({"ok": True, "public_path": f"/report/{client_id}"})


@app.get("/report/<client_id>")
def report_page(client_id):
    """
    Serves YOUR UI (templates/report.html).
    The UI will fetch JSON from /api/report/<client_id>.
    """
    con = db()
    client = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    payload_row = con.execute("SELECT payload FROM payloads WHERE client_id = ?", (client_id,)).fetchone()
    con.close()

    if not client or not payload_row:
        return "No data available yet. Please contact provider.", 404

    allowed, msg = ensure_access_window(client)
    if not allowed:
        return f"⛔ Access refused: {msg}", 403

    # Re-read after possible update
    con = db()
    client2 = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    payload = json.loads(payload_row["payload"])
    con.close()

    year = payload.get("year", "")
    # Your HTML figures out client_id from URL, but we can still pass metadata if you want.
    return render_template(
        "report.html",
        client_id=client_id,
        year=year,
        window_expires_at=client2["window_expires_at"],
        views_used=client2["views_used"],
        max_views=MAX_VIEWS
    )


@app.get("/api/report/<client_id>")
def report_api(client_id):
    """
    Returns JSON for the UI.
    Only accessible if within current access window.
    """
    con = db()
    client = con.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    payload_row = con.execute("SELECT payload, updated_at FROM payloads WHERE client_id = ?", (client_id,)).fetchone()
    con.close()

    if not client or not payload_row:
        abort(404)

    # must be active trial
    if not trial_is_active(client["trial_start"]):
        abort(403)

    # must be inside current window
    window_exp = parse_dt(client["window_expires_at"])
    if window_exp is None or now_utc() > window_exp:
        abort(403)

    payload = json.loads(payload_row["payload"])
    updated_at = payload_row["updated_at"]

    # Friendly updated_at string for your header
    try:
        dt = datetime.fromisoformat(updated_at)
        updated_str = dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    except Exception:
        updated_str = updated_at

    # Ensure shape expected by your HTML
    # payload must contain "data" dict
    data = payload.get("data", {})
    year = payload.get("year", "")

    return jsonify({
        "client": client_id,
        "year": year,
        "updated_at": updated_str,
        "trial": {
            "valid_until": client["window_expires_at"],
            "views_used": client["views_used"],
            "views_max": MAX_VIEWS
        },
        "data": data
    })


# Local dev only (Render uses gunicorn)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
