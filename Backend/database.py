"""SQLite storage for construction sites."""

from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import json
# Implements the storage functions

DATABASE_PATH = Path(
    os.environ.get("PLANIT_DATABASE_PATH", Path(__file__).with_name("sites.db"))
)
CATALOG_PATH = Path(__file__).with_name("data") / "eia_plants.json"
SITE_METADATA = {
    "eia_plant_id": "INTEGER", "utility": "TEXT", "county": "TEXT",
    "state": "TEXT", "plant_type": "TEXT", "source": "TEXT",
    "source_url": "TEXT", "source_period": "TEXT",
}

# Opens a database conects, commits successful changes, rolls back
# failed changes, closes connection afterwards.
# Handles storage and ID assignment
@contextmanager
def connect():
    # Each operation gets its own connection, safe for FastAPI's worker threads.
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()

# Creates the sites table if needed. Adds demo site without overwritting
def initialize_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180)
            )
            """
        )
        # Preserve the existing demo site without overwriting saved data.
        connection.execute(
            """
            INSERT OR IGNORE INTO sites (id, name, latitude, longitude)
            VALUES (?, ?, ?, ?)
            """,
            (1, "Demo Job Site", 25.75, -80.70),
        )
        # Add metadata to existing installations without changing saved site IDs.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sites)")}
        for name, kind in SITE_METADATA.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE sites ADD COLUMN {name} {kind}")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS sites_eia_id ON sites(eia_plant_id)")
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        for plant in catalog["plants"]:
            connection.execute(
                """INSERT INTO sites
                (name, latitude, longitude, eia_plant_id, utility, county, state,
                 plant_type, source, source_url, source_period)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(eia_plant_id) DO UPDATE SET
                    name=excluded.name, latitude=excluded.latitude, longitude=excluded.longitude,
                    utility=excluded.utility, county=excluded.county, state=excluded.state,
                    plant_type=excluded.plant_type, source=excluded.source,
                    source_url=excluded.source_url, source_period=excluded.source_period""",
                (plant["name"], plant["latitude"], plant["longitude"], plant["eia_plant_id"],
                 plant["utility"], plant["county"], plant["state"], plant["plant_type"],
                 catalog["source"], catalog["source_url"], catalog["source_period"]),
            )
        connection.execute("""CREATE TABLE IF NOT EXISTS risk_snapshots (
            site_id INTEGER PRIMARY KEY,
            generated_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS provider_cache (
            cache_key TEXT PRIMARY KEY,
            expires_at REAL NOT NULL,
            payload TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS metar_station_selection (
            site_id INTEGER PRIMARY KEY, latitude REAL NOT NULL, longitude REAL NOT NULL,
            station_id TEXT NOT NULL, station_latitude REAL NOT NULL,
            station_longitude REAL NOT NULL, distance_km REAL NOT NULL,
            provenance TEXT NOT NULL, selected_at TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS refresh_leases (
            name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, trigger TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            site_count INTEGER NOT NULL DEFAULT 0, failures TEXT NOT NULL DEFAULT '[]'
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS refresh_site_status (
            kind TEXT NOT NULL, site_id INTEGER NOT NULL, last_attempt_at TEXT,
            last_success_at TEXT, last_failure_at TEXT, last_error TEXT,
            PRIMARY KEY (kind, site_id)
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS initial_assessments (
            site_id INTEGER PRIMARY KEY, status TEXT NOT NULL, queued_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT, error TEXT, source_failures TEXT NOT NULL DEFAULT '[]'
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS lightning_snapshots (
            site_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS refresh_job_status (
            kind TEXT PRIMARY KEY, last_attempt_at TEXT, last_success_at TEXT,
            last_failure_at TEXT, last_error TEXT
        )""")
    import notifications, alerts, strike_feed, crew_reports, briefings, rivers, hefs
    for module in (notifications, alerts, strike_feed, crew_reports, briefings, rivers, hefs):
        module.initialize()

# Retrieves all sties
def list_sites():
    with connect() as connection:
        rows = connection.execute("SELECT * FROM sites ORDER BY id").fetchall()
        return [dict(row) for row in rows]

# Retrieves one site by ID
def find_site(site_id: int):
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM sites WHERE id = ?", (site_id,)
        ).fetchone()
        return dict(row) if row is not None else None

# Saves a asite. Returns it with ID
def insert_site(name: str, latitude: float, longitude: float):
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO sites (name, latitude, longitude) VALUES (?, ?, ?)",
            (name, latitude, longitude),
        )
        row = connection.execute(
            "SELECT * FROM sites WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        from risk_sources import utcnow
        connection.execute("INSERT INTO initial_assessments (site_id, status, queued_at) VALUES (?, 'pending', ?)",
                           (row['id'], utcnow().isoformat()))
        return dict(row)


def initial_assessment(site_id):
    with connect() as connection:
        row = connection.execute('SELECT * FROM initial_assessments WHERE site_id=?', (site_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['source_failures'] = json.loads(result['source_failures'])
    return result


def due_initial_assessments(now):
    from risk_sources import parse_time
    abandoned_before = (parse_time(now) - timedelta(minutes=3)).isoformat()
    with connect() as connection:
        return [row['site_id'] for row in connection.execute("""SELECT site_id FROM initial_assessments
            WHERE status='pending' OR (status='failed' AND next_retry_at<=?)
                OR (status='running' AND started_at<=?) ORDER BY queued_at LIMIT 20""",
            (now, abandoned_before))]


def claim_initial_assessment(site_id, now):
    from risk_sources import parse_time
    abandoned_before = (parse_time(now) - timedelta(minutes=3)).isoformat()
    with connect() as connection:
        cursor = connection.execute("""UPDATE initial_assessments SET status='running', started_at=?,
            finished_at=NULL, attempts=attempts+1, error=NULL, next_retry_at=NULL
            WHERE site_id=? AND (status='pending' OR (status='failed' AND next_retry_at<=?)
                OR (status='running' AND started_at<=?))""",
            (now, site_id, now, abandoned_before))
        return cursor.rowcount == 1


def finish_initial_assessment(site_id, started_at, now, status, error=None, source_failures=None, next_retry_at=None):
    with connect() as connection:
        connection.execute("""UPDATE initial_assessments SET status=?, finished_at=?, error=?,
            source_failures=?, next_retry_at=? WHERE site_id=? AND status='running' AND started_at=?""",
            (status, now, error, json.dumps(source_failures or []), next_retry_at, site_id, started_at))


def retry_initial_assessment(site_id, now):
    with connect() as connection:
        return connection.execute("""UPDATE initial_assessments SET status='pending', queued_at=?,
            started_at=NULL, finished_at=NULL, next_retry_at=NULL, error=NULL
            WHERE site_id=? AND status='failed'""", (now, site_id)).rowcount == 1


def save_risk(site_id, risk):
    with connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO risk_snapshots VALUES (?, ?, ?)",
            (site_id, risk["generated_at"], json.dumps(risk, allow_nan=False)),
        )


def load_risk(site_id):
    with connect() as connection:
        row = connection.execute("SELECT payload FROM risk_snapshots WHERE site_id = ?", (site_id,)).fetchone()
        return json.loads(row["payload"]) if row else None


def save_refreshed_risk(site_id, risk):
    import ml_risk

    with connect() as connection:
        # Read the newest ML submission after provider requests finish. Hold the
        # write lock while retaining it so a refresh cannot erase an intervening result.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload FROM risk_snapshots WHERE site_id = ?", (site_id,)).fetchone()
        previous = json.loads(row["payload"]) if row else None
        risk["ml_assessment"] = ml_risk.retain_on_refresh(previous, risk)
        connection.execute("INSERT OR REPLACE INTO risk_snapshots VALUES (?, ?, ?)",
                           (site_id, risk["generated_at"], json.dumps(risk, allow_nan=False)))
    return risk


def save_model_output(site_id, payload):
    import ml_risk
    from risk_sources import utcnow
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload FROM risk_snapshots WHERE site_id=?", (site_id,)).fetchone()
        if not row:
            raise ValueError("Export an assessment before submitting model output")
        assessment = json.loads(row["payload"])
        incoming = ml_risk.parse_prediction(payload, ml_risk.request_for(assessment))
        model = ml_risk.merge_predictions(assessment.get("ml_assessment"), incoming)
        model.update(stale=False, received_at=utcnow().isoformat(), input_generated_at=assessment["generated_at"])
        assessment["ml_assessment"] = model
        connection.execute("UPDATE risk_snapshots SET payload=? WHERE site_id=?", (json.dumps(assessment), site_id))
        return model


def load_provider_cache(key, now):
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM provider_cache WHERE cache_key = ? AND expires_at > ?", (key, now)
        ).fetchone()
        return json.loads(row["payload"]) if row else None


def save_provider_cache(key, expires_at, payload):
    with connect() as connection:
        connection.execute("DELETE FROM provider_cache WHERE expires_at < ?", (expires_at - 86400,))
        connection.execute("INSERT OR REPLACE INTO provider_cache VALUES (?, ?, ?)",
                           (key, expires_at, json.dumps(payload, allow_nan=False)))


def metar_station(site):
    with connect() as connection:
        row = connection.execute("SELECT * FROM metar_station_selection WHERE site_id=?", (site['id'],)).fetchone()
        if row and (row['latitude'], row['longitude']) == (site['latitude'], site['longitude']):
            return dict(row)
        return None


def save_metar_station(site, station):
    with connect() as connection:
        connection.execute("""INSERT OR REPLACE INTO metar_station_selection VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (site['id'], site['latitude'], site['longitude'],
            station['station_id'], station['station_latitude'], station['station_longitude'],
            station['distance_km'], station['provenance'], station['selected_at']))


def claim_lease(name, owner, now, ttl):
    with connect() as connection:
        cursor = connection.execute(
            """INSERT INTO refresh_leases VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
            WHERE refresh_leases.expires_at <= ?""", (name, owner, now + ttl, now))
        return cursor.rowcount == 1


def renew_lease(name, owner, now, ttl):
    with connect() as connection:
        return connection.execute(
            "UPDATE refresh_leases SET expires_at=? WHERE name=? AND owner=? AND expires_at>?",
            (now + ttl, name, owner, now)).rowcount == 1


def release_lease(name, owner):
    with connect() as connection:
        connection.execute("DELETE FROM refresh_leases WHERE name=? AND owner=?", (name, owner))


def latest_run(kind):
    with connect() as connection:
        row = connection.execute("SELECT * FROM refresh_runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return dict(row) if row else None


def start_run(kind, trigger, timestamp, site_count):
    with connect() as connection:
        connection.execute("""INSERT INTO refresh_job_status (kind, last_attempt_at) VALUES (?, ?)
            ON CONFLICT(kind) DO UPDATE SET last_attempt_at=excluded.last_attempt_at""", (kind, timestamp))
        # Called only while holding this kind's lease. A leftover running record
        # belongs to a process that crashed and whose lease has expired.
        connection.execute("""UPDATE refresh_runs SET status='interrupted', finished_at=?,
            failures=? WHERE kind=? AND status='running'""",
            (timestamp, json.dumps([{"error": "Previous worker stopped before completing the run"}]), kind))
        cursor = connection.execute("""INSERT INTO refresh_runs
            (kind, trigger, started_at, status, site_count) VALUES (?, ?, ?, 'running', ?)""",
            (kind, trigger, timestamp, site_count))
        return cursor.lastrowid


def finish_run(run_id, timestamp, status, failures):
    with connect() as connection:
        row = connection.execute("SELECT kind FROM refresh_runs WHERE id=?", (run_id,)).fetchone()
        if row:
            if status == "succeeded":
                connection.execute("UPDATE refresh_job_status SET last_success_at=?, last_error=NULL WHERE kind=?",
                                   (timestamp, row["kind"]))
            else:
                connection.execute("UPDATE refresh_job_status SET last_failure_at=?, last_error=? WHERE kind=?",
                                   (timestamp, json.dumps(failures), row["kind"]))
        connection.execute("UPDATE refresh_runs SET finished_at=?, status=?, failures=? WHERE id=?",
                           (timestamp, status, json.dumps(failures), run_id))
        connection.execute("DELETE FROM refresh_runs WHERE id NOT IN (SELECT id FROM refresh_runs ORDER BY id DESC LIMIT 1000)")


def record_site_refresh(kind, site_id, timestamp, error=None):
    with connect() as connection:
        connection.execute("""INSERT INTO refresh_site_status
            (kind, site_id, last_attempt_at, last_success_at, last_failure_at, last_error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, site_id) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=COALESCE(excluded.last_success_at, refresh_site_status.last_success_at),
                last_failure_at=COALESCE(excluded.last_failure_at, refresh_site_status.last_failure_at),
                last_error=excluded.last_error""",
            (kind, site_id, timestamp, timestamp if error is None else None,
             timestamp if error is not None else None, error))


def refresh_status():
    with connect() as connection:
        runs = [dict(row) for row in connection.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 20")]
        for run in runs:
            run["failures"] = json.loads(run["failures"])
        return {
            "recent_runs": runs,
            "sites": [dict(row) for row in connection.execute("SELECT * FROM refresh_site_status ORDER BY site_id, kind")],
            "leases": [dict(row) for row in connection.execute("SELECT name, expires_at FROM refresh_leases")],
            "jobs": [dict(row) for row in connection.execute("SELECT * FROM refresh_job_status ORDER BY kind")],
            "last_successful_runs": {row["kind"]: row["last_success_at"] for row in connection.execute(
                "SELECT kind, last_success_at FROM refresh_job_status")},
        }


def save_lightning(site_id, payload):
    with connect() as connection:
        connection.execute("INSERT OR REPLACE INTO lightning_snapshots VALUES (?, ?)",
                           (site_id, json.dumps(payload, allow_nan=False)))


def load_lightning(site_id):
    with connect() as connection:
        row = connection.execute("SELECT payload FROM lightning_snapshots WHERE site_id=?", (site_id,)).fetchone()
        return json.loads(row["payload"]) if row else None
