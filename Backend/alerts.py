"""Application alert episodes and required actions, independent of ML scores."""

from datetime import datetime, timezone
import json
from fastapi import APIRouter, HTTPException, Query
import database
import notifications

router = APIRouter(prefix="/api", tags=["Alerts"])
RANK = {"unknown": -1, "info": -1, "low": 0, "watch": 1, "high": 2, "severe": 3}
ACTIONS = {
    "lightning_strike": "Stop outdoor work and shelter. Resume only after the 30-minute hold expires and measured coverage confirms clearance.",
    "lightning": "Pause exposed work, seek suitable shelter, and assess thunderstorm warnings before resuming.",
    "flooding": "Suspend work in threatened low areas; move crews and equipment to higher ground and review evacuation routes.",
    "extreme_heat": "Apply the site heat plan: shade, water, rest breaks, and supervisor review before exposed work continues.",
    "weather": "Pause weather-exposed operations and have a supervisor review the active weather hazard.",
    "wildfire": "Review fire warnings and evacuation routes; suspend work in affected areas.",
    "soil_saturation": "Pause heavy equipment access until ground bearing capacity and route safety are checked.",
}


def initialize():
    with database.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
            hazard TEXT NOT NULL, origin TEXT NOT NULL, message TEXT NOT NULL,
            severity TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, resolved_at TEXT, stop_work INTEGER NOT NULL,
            required_action TEXT NOT NULL, evidence TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_alert_episode
            ON alerts(site_id,hazard,origin) WHERE status='active'""")


def serialize(row):
    return {**dict(row), "stop_work": bool(row["stop_work"]), "evidence": json.loads(row["evidence"])}


def upsert(site_id, hazard, severity, message, evidence, *, origin="rule_based", stop_work=False,
           now=None, notify_update=False, connection=None):
    if connection is None:
        with database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return upsert(site_id, hazard, severity, message, evidence, origin=origin, stop_work=stop_work,
                          now=now, notify_update=notify_update, connection=conn)
    now = (now or datetime.now(timezone.utc)).isoformat()
    row = connection.execute("SELECT * FROM alerts WHERE site_id=? AND hazard=? AND origin=? AND status='active'",
                             (site_id, hazard, origin)).fetchone()
    action = ACTIONS.get(hazard, "Have a supervisor assess the hazard before work continues.")
    changed = row is None or RANK[severity] > RANK[row["severity"]] or notify_update
    if row is None:
        cursor = connection.execute("""INSERT INTO alerts
            (site_id,hazard,origin,message,severity,created_at,updated_at,status,stop_work,required_action,evidence)
            VALUES (?,?,?,?,?,?,?,'active',?,?,?)""",
            (site_id, hazard, origin, message, severity, now, now, int(stop_work), action, json.dumps(evidence)))
        alert_id = cursor.lastrowid
    else:
        alert_id = row["id"]
        connection.execute("""UPDATE alerts SET message=?,severity=?,updated_at=?,stop_work=?,evidence=?,
            revision=revision+? WHERE id=?""",
            (message, severity, now, int(stop_work), json.dumps(evidence), int(changed), alert_id))
    result = serialize(connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())
    if changed:
        notifications.publish(site_id, "alert_created" if row is None else "alert_updated", {"alert": result},
                              f"alert:{alert_id}:{result['revision']}", connection=connection)
    return result


def resolve(site_id, hazard, origin="rule_based", now=None, connection=None):
    if connection is None:
        with database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return resolve(site_id, hazard, origin, now, conn)
    row = connection.execute("SELECT * FROM alerts WHERE site_id=? AND hazard=? AND origin=? AND status='active'",
                             (site_id, hazard, origin)).fetchone()
    if row is None:
        return
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    connection.execute("UPDATE alerts SET status='resolved',resolved_at=?,updated_at=? WHERE id=?",
                       (stamp, stamp, row["id"]))
    result = serialize(connection.execute("SELECT * FROM alerts WHERE id=?", (row["id"],)).fetchone())
    notifications.publish(site_id, "alert_resolved", {"alert": result}, f"alert:{row['id']}:resolved", connection=connection)


def for_site(site_id, status="active"):
    with database.connect() as conn:
        rows = conn.execute("SELECT * FROM alerts WHERE site_id=? AND (? IS NULL OR status=?) ORDER BY id DESC",
                            (site_id, status, status)).fetchall()
    return [serialize(row) for row in rows]


def required_actions(site_id):
    return [{"alert_id": row["id"], "hazard": row["hazard"], "action": row["required_action"],
             "stop_work": row["stop_work"], "basis": row["origin"]} for row in for_site(site_id)]


def evaluate(assessment, include_lightning=True, allow_resolution=True):
    site_id = assessment["site"]["id"]
    for hazard, card in assessment["current_risk"]["indicators"].items():
        if hazard == "lightning" and not include_lightning:
            continue  # The faster monitor owns this episode; old full-feed caches cannot clear it.
        level = card["level"]
        if RANK.get(level, -1) >= 2:
            # The preliminary pass emits independent alerts before slower river
            # requests. It must not downgrade an existing river-assisted episode
            # and then re-escalate it on every refresh.
            if not allow_resolution:
                previous = next((a for a in for_site(site_id) if a['hazard'] == hazard
                                 and a['origin'] == 'rule_based'), None)
                if previous and RANK[previous['severity']] >= RANK[level]:
                    continue
            upsert(site_id, hazard, level, f"{hazard.replace('_', ' ').capitalize()}: {card['basis']}",
                   {**card["evidence"], "assessed_at": assessment["generated_at"],
                    "sources": assessment.get("sources", {})},
                   stop_work=hazard in ("lightning", "flooding", "weather", "wildfire") or level == "severe")
        elif allow_resolution and card.get("coverage") == "available" and level in ("low", "watch"):
            resolve(site_id, hazard)
        elif allow_resolution and hazard == "lightning" and card.get("thunderstorm_forecast", {}).get("availability") == "available" and level != "high":
            # The forecast episode is distinct from measured-strike hold state.
            if assessment.get("sources", {}).get("alerts", {}).get("status") == "available":
                resolve(site_id, hazard)


@router.get("/sites/{site_id}/alerts")
def get_alerts(site_id: int, status: str | None = Query("active", pattern="^(active|resolved)$")):
    if not database.find_site(site_id):
        raise HTTPException(404, "Site not found")
    return {"site_id": site_id, "alerts": for_site(site_id, status), "required_safety_actions": required_actions(site_id)}
