import os
import json
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, abort, render_template
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

# ==========================================================
# Render Flask app (PostgreSQL persistent) ✅
#
# Endpoints:
#   GET  /health
#   POST /push-data           (X-API-KEY required)
#   GET  /report/<client_id>  (serves your UI template)
#   GET  /api/report/<client_id> (UI fetches JSON here)
#
# Persists "last JSON" in PostgreSQL so sleep/restarts don't lose data.
# ==========================================================

app = Flask(__name__)

# ---- Required env vars on Render ----
API_KEY = os.getenv("API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # Render provides this for Postgres

# ---- Trial settings (tweak on Render dashboard) ----
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))                 # total trial lifetime from first push
MAX_VIEWS = int(os.getenv("MAX_VIEWS", "3"))                   # number of 24h windows allowed
HTML_VALID_HOURS = int(os.getenv("HTML_VALID_HOURS", "24"))    # length of each window

# ---- Connection pool ----
POOL_MIN = int(os.getenv("PG_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("PG_POOL_MAX", "5"))

_pool: SimpleConnectionPool | None = None


# -------------------- Helpers --------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def require_api_key():
    if not API_KEY:
        abort(500, description="API_KEY not configured on server")
    if request.headers.get("X-API-KEY", "") != API_KEY:
        abort(403, description="Unauthorized")


def get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL not configured (Render Postgres not attached?)")
        _pool = SimpleConnectionPool(
            POOL_MIN,
            POOL_MAX,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def db_conn():
    pool = get_pool()
    return pool.getconn()


def db_putconn(conn):
    pool = get_pool()
    pool.putconn(conn)


def init_db():
    """Create tables if they don't exist (idempotent)."""
    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    trial_start TIMESTAMPTZ NOT NULL,
                    views_used INTEGER NOT NULL DEFAULT 0,
                    window_expires_at TIMESTAMPTZ
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payloads (
                    client_id TEXT PRIMARY KEY,
                    payload_json JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()
    finally:
        if conn is not None:
            db_putconn(conn)


@app.before_request
def _ensure_db_ready():
    # Safe to call often; quick once tables exist.
    init_db()


# -------------------- Trial / window logic --------------------
def trial_is_active(trial_start: datetime) -> bool:
    return utc_now() <= (trial_start + timedelta(days=TRIAL_DAYS))


def ensure_access_window(client_id: str) -> tuple[bool, str, dict | None]:
    """
    Ensures a valid 24h access window exists for this client.
    - If trial ended => deny
    - If window missing/expired => consume a view and open a new window (if views remain)
    Returns (allowed, message, updated_client_row)
    """
    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clients WHERE client_id=%s", (client_id,))
            row = cur.fetchone()
            if not row:
                return False, "No client", None

            trial_start = row["trial_start"]
            if isinstance(trial_start, str):
                trial_start = parse_iso(trial_start)  # just in case
            if not trial_start or not trial_is_active(trial_start):
                return False, "Trial ended", row

            now = utc_now()
            window_exp = row.get("window_expires_at")
            if isinstance(window_exp, str):
                window_exp = parse_iso(window_exp)

            # If no window or expired -> consume a view and open new window
            if (window_exp is None) or (now > window_exp):
                if row["views_used"] >= MAX_VIEWS:
                    return False, "Trial limit reached", row

                new_exp = now + timedelta(hours=HTML_VALID_HOURS)
                cur.execute("""
                    UPDATE clients
                    SET views_used = views_used + 1,
                        window_expires_at = %s
                    WHERE client_id = %s
                    RETURNING *;
                """, (new_exp, client_id))
                conn.commit()
                updated = cur.fetchone()
                return True, "OK", updated

            return True, "OK", row
    finally:
        if conn is not None:
            db_putconn(conn)


def require_active_window_or_403(client_id: str):
    allowed, msg, row = ensure_access_window(client_id)
    if not allowed:
        abort(403, description=msg)
    return row


# -------------------- Routes --------------------
@app.get("/health")
def health():
    # Also checks DB connectivity quickly
    try:
        init_db()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.get("/")
def home():
    return {
        "ok": True,
        "service": "sage-reporting-program",
        "endpoints": {
            "health": "/health",
            "push_data": "POST /push-data (X-API-KEY required)",
            "report_page": "/report/<client_id>",
            "report_api": "/api/report/<client_id>",
        }
    }


@app.post("/push-data")
def push_data():
    """
    Your local script uploads the computed dashboard JSON here.
    Header: X-API-KEY: <API_KEY>

    Expected payload:
    {
      "client_id": "RELAISMEDICAL",
      "year": 2025,
      "data": { ... }
    }
    """
    require_api_key()

    payload = request.get_json(force=True) or {}
    client_id = str(payload.get("client_id", "")).strip()
    if not client_id:
        return jsonify({"ok": False, "error": "client_id is required"}), 400

    data = payload.get("data", None)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "data must be an object/dict"}), 400

    now = utc_now()

    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            # Create client if new
            cur.execute("SELECT client_id FROM clients WHERE client_id=%s", (client_id,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute("""
                    INSERT INTO clients (client_id, trial_start, views_used, window_expires_at)
                    VALUES (%s, %s, 0, NULL);
                """, (client_id, now))

            # Upsert payload
            cur.execute("""
                INSERT INTO payloads (client_id, payload_json, updated_at)
                VALUES (%s, %s::jsonb, %s)
                ON CONFLICT (client_id)
                DO UPDATE SET payload_json = EXCLUDED.payload_json, updated_at = EXCLUDED.updated_at;
            """, (client_id, json.dumps(payload, ensure_ascii=False), now))

        conn.commit()
    finally:
        if conn is not None:
            db_putconn(conn)

    return jsonify({"ok": True, "public_path": f"/report/{client_id}"})


@app.get("/report/<client_id>")
def report_page(client_id: str):
    """
    Serves your exact UI from templates/report.html
    The UI fetches data from /api/report/<client_id>.
    """
    client_id = client_id.strip()

    # Must have payload stored
    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT payload_json FROM payloads WHERE client_id=%s", (client_id,))
            prow = cur.fetchone()
            if not prow:
                return "No data available yet. Please contact provider.", 404
    finally:
        if conn is not None:
            db_putconn(conn)

    # Enforce window/view logic (this consumes a view when needed)
    row = require_active_window_or_403(client_id)

    # Provide some optional meta into template (your HTML can ignore these)
    year = None
    try:
        # We can read year without an extra DB hit by re-reading payload quickly
        payload = prow["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        year = payload.get("year")
    except Exception:
        year = None

    return render_template(
        "report.html",
        client_id=client_id,
        year=year or "",
        window_expires_at=iso(row["window_expires_at"]) if row.get("window_expires_at") else "",
        views_used=row.get("views_used", 0),
        max_views=MAX_VIEWS
    )


@app.get("/api/report/<client_id>")
def report_api(client_id: str):
    """
    UI loads JSON from here.
    IMPORTANT: We require the client to be within the active 24h window.
    (We do NOT consume additional views here; views are managed by /report.)
    """
    client_id = client_id.strip()

    # Verify client exists & trial active & within current window
    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clients WHERE client_id=%s", (client_id,))
            client = cur.fetchone()
            if not client:
                abort(404)

            trial_start = client["trial_start"]
            if isinstance(trial_start, str):
                trial_start = parse_iso(trial_start)
            if not trial_start or not trial_is_active(trial_start):
                abort(403, description="Trial ended")

            window_exp = client.get("window_expires_at")
            if isinstance(window_exp, str):
                window_exp = parse_iso(window_exp)

            if (window_exp is None) or (utc_now() > window_exp):
                abort(403, description="Access window expired")

            cur.execute("SELECT payload_json, updated_at FROM payloads WHERE client_id=%s", (client_id,))
            row = cur.fetchone()
            if not row:
                abort(404)

            payload = row["payload_json"]
            updated_at = row["updated_at"]

    finally:
        if conn is not None:
            db_putconn(conn)

    # Format updated_at for your header
    try:
        if isinstance(updated_at, str):
            updated_dt = parse_iso(updated_at) or utc_now()
        else:
            updated_dt = updated_at
        updated_str = updated_dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    except Exception:
        updated_str = str(updated_at)

    if isinstance(payload, str):
        payload = json.loads(payload)

    data = payload.get("data", {})
    year = payload.get("year", "")

    return jsonify({
        "client": client_id,
        "year": year,
        "updated_at": updated_str,
        "trial": {
            "valid_until": iso(window_exp) if window_exp else "",
            "views_used": client.get("views_used", 0),
            "views_max": MAX_VIEWS
        },
        "data": data
    })


# ---- local dev only ----
if __name__ == "__main__":
    # For local testing: export DATABASE_URL and API_KEY then run python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
