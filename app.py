"""
UPDATED app.py for Render with Excel Download Support

Changes from original:
1. Stores excel_filename and excel_b64 in payloads table
2. Returns Excel data in /api/report/<client_id> endpoint

DEPLOYMENT:
1. Replace your app.py on Render with this version
2. Add migration to update payloads table (see below)
3. Redeploy
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, abort, render_template
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

app = Flask(__name__)

# ---- Required env vars on Render ----
API_KEY = os.getenv("API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ---- Trial settings ----
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
MAX_VIEWS = int(os.getenv("MAX_VIEWS", "3"))
HTML_VALID_HOURS = int(os.getenv("HTML_VALID_HOURS", "24"))

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
            raise RuntimeError("DATABASE_URL not configured")
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
    """Create tables if they don't exist"""
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
            
            # Updated payloads table with Excel support
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payloads (
                    client_id TEXT PRIMARY KEY,
                    payload_json JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    excel_filename TEXT,
                    excel_b64 TEXT
                );
            """)
            
            # Add columns if they don't exist (migration)
            cur.execute("""
                DO $$ 
                BEGIN
                    BEGIN
                        ALTER TABLE payloads ADD COLUMN excel_filename TEXT;
                    EXCEPTION
                        WHEN duplicate_column THEN NULL;
                    END;
                    BEGIN
                        ALTER TABLE payloads ADD COLUMN excel_b64 TEXT;
                    EXCEPTION
                        WHEN duplicate_column THEN NULL;
                    END;
                END $$;
            """)
            
        conn.commit()
    finally:
        if conn is not None:
            db_putconn(conn)


@app.before_request
def _ensure_db_ready():
    init_db()


# -------------------- Trial logic --------------------
def trial_is_active(trial_start: datetime) -> bool:
    return utc_now() <= (trial_start + timedelta(days=TRIAL_DAYS))


def ensure_access_window(client_id: str) -> tuple[bool, str, dict | None]:
    """Ensures a valid 24h access window exists"""
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
                trial_start = parse_iso(trial_start)
            if not trial_start or not trial_is_active(trial_start):
                return False, "Trial ended", row

            now = utc_now()
            window_exp = row.get("window_expires_at")
            if isinstance(window_exp, str):
                window_exp = parse_iso(window_exp)

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


@app.route("/push-data", methods=["POST"])
def push_data():
    """
    Upload dashboard JSON + optional Excel file (base64)
    
    Expected payload:
    {
      "client_id": "RELAISMEDICAL",
      "year": 2025,
      "data": { ... },
      "excel_filename": "ESG_Report.xlsx",  // optional
      "excel_b64": "base64..."              // optional
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

    # Optional Excel data
    excel_filename = payload.get("excel_filename")
    excel_b64 = payload.get("excel_b64")

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

            # Upsert payload with Excel data
            cur.execute("""
                INSERT INTO payloads (client_id, payload_json, updated_at, excel_filename, excel_b64)
                VALUES (%s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (client_id)
                DO UPDATE SET 
                    payload_json = EXCLUDED.payload_json, 
                    updated_at = EXCLUDED.updated_at,
                    excel_filename = EXCLUDED.excel_filename,
                    excel_b64 = EXCLUDED.excel_b64;
            """, (client_id, json.dumps(payload, ensure_ascii=False), now, excel_filename, excel_b64))

        conn.commit()
    finally:
        if conn is not None:
            db_putconn(conn)

    return jsonify({"ok": True, "public_path": f"/report/{client_id}"})


@app.get("/report/<client_id>")
def report_page(client_id: str):
    """Serves report.html template"""
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

    # Enforce window/view logic
    row = require_active_window_or_403(client_id)

    year = None
    try:
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
    Returns JSON data for report.html
    Includes Excel download data if available
    """
    client_id = client_id.strip()

    conn = None
    try:
        conn = db_conn()
        with conn.cursor() as cur:
            # Verify client
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

            # Get payload with Excel data
            cur.execute("""
                SELECT payload_json, updated_at, excel_filename, excel_b64 
                FROM payloads 
                WHERE client_id=%s
            """, (client_id,))
            row = cur.fetchone()
            if not row:
                abort(404)

            payload = row["payload_json"]
            updated_at = row["updated_at"]
            excel_filename = row.get("excel_filename")
            excel_b64 = row.get("excel_b64")

    finally:
        if conn is not None:
            db_putconn(conn)

    # Format updated_at
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

    response = {
        "client": client_id,
        "year": year,
        "updated_at": updated_str,
        "trial": {
            "valid_until": iso(window_exp) if window_exp else "",
            "views_used": client.get("views_used", 0),
            "views_max": MAX_VIEWS
        },
        "data": data
    }

    # Add Excel data if available
    if excel_filename and excel_b64:
        response["excel_filename"] = excel_filename
        response["excel_b64"] = excel_b64

    return jsonify(response)


# ---- local dev only ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))