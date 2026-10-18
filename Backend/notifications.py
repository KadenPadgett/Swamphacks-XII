"""Transactional, durable in-app events and site-scoped resumable SSE delivery."""

import asyncio
from datetime import datetime, timezone
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
import database

router = APIRouter(prefix="/api", tags=["Notifications"])


def initialize():
    with database.connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE)""")
        conn.execute("CREATE INDEX IF NOT EXISTS notification_site ON notifications(site_id,id)")


def publish(site_id, kind, payload, dedupe_key, connection=None):
    if connection is None:
        with database.connect() as conn:
            return publish(site_id, kind, payload, dedupe_key, connection=conn)
    connection.execute("""INSERT OR IGNORE INTO notifications
        (site_id,kind,payload,created_at,dedupe_key) VALUES (?,?,?,?,?)""",
        (site_id, kind, json.dumps(payload, allow_nan=False), datetime.now(timezone.utc).isoformat(), dedupe_key))
    return connection.execute("SELECT id FROM notifications WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]


def events(site_id, after=0, limit=100):
    with database.connect() as conn:
        rows = conn.execute("SELECT * FROM notifications WHERE site_id=? AND id>? ORDER BY id LIMIT ?",
                            (site_id, after, limit)).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def require_site(site_id):
    if not database.find_site(site_id):
        raise HTTPException(404, "Site not found")


@router.get("/sites/{site_id}/notifications")
def get_notifications(site_id: int, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    require_site(site_id)
    rows = events(site_id, after, limit)
    return {"site_id": site_id, "notifications": rows, "next_cursor": rows[-1]["id"] if rows else after}


async def event_stream(site_id, after, request):
    # Query durable storage rather than a process-local queue: reconnects and
    # multiple workers see the same events. Delivery is at least once by event ID.
    while not await request.is_disconnected():
        rows = events(site_id, after)
        for event in rows:
            yield f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
            after = event["id"]
        if not rows:
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)


@router.get("/sites/{site_id}/events")
async def subscribe(site_id: int, request: Request, after: int = Query(0, ge=0)):
    require_site(site_id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError as error:
        raise HTTPException(422, "Last-Event-ID must be a nonnegative integer") from error
    if cursor < 0:
        raise HTTPException(422, "Last-Event-ID must be a nonnegative integer")
    return StreamingResponse(event_stream(site_id, cursor, request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
