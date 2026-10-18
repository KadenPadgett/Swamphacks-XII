"""Persistent crew observations and photo uploads, independent of model availability."""

from datetime import datetime, timezone
from io import BytesIO
import math
import os
from pathlib import Path
import uuid
import warnings

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

import database
import notifications


router = APIRouter(prefix="/api", tags=["Crew reports"])
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_PHOTO_PIXELS = 20_000_000
ACCEPTED_PHOTO_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def upload_directory():
    return Path(os.environ.get("PLANIT_UPLOAD_DIR", Path(__file__).parent / "data" / "uploads"))


def initialize():
    with database.connect() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS crew_photos (
            id TEXT PRIMARY KEY, site_id INTEGER NOT NULL, filename TEXT NOT NULL,
            media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS crew_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
            hazard_type TEXT NOT NULL, description TEXT NOT NULL,
            latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
            longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
            photo_reference TEXT, report_time TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
            resolved_at TEXT
        )""")
        connection.execute("CREATE INDEX IF NOT EXISTS crew_report_site_status ON crew_reports(site_id, status, report_time)")
        connection.execute("CREATE INDEX IF NOT EXISTS crew_report_coordinates ON crew_reports(latitude, longitude)")


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    hazard_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_ -]*$")
    description: str = Field(min_length=1, max_length=4000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    photo_reference: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


def require_site(site_id):
    if database.find_site(site_id) is None:
        raise HTTPException(status_code=404, detail="Site not found")


def serialize_report(row):
    report = dict(row)
    report["photo_url"] = f"/api/photos/{report['photo_reference']}" if report["photo_reference"] else None
    report["source"] = "crew_report"
    return report


def list_reports(site_id, status=None):
    with database.connect() as connection:
        if status is None:
            rows = connection.execute(
                "SELECT * FROM crew_reports WHERE site_id = ? ORDER BY report_time DESC, id DESC", (site_id,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM crew_reports WHERE site_id = ? AND status = ? ORDER BY report_time DESC, id DESC",
                (site_id, status),
            ).fetchall()
    return [serialize_report(row) for row in rows]


def active_reports(site_id):
    return list_reports(site_id, "active")


def _distance_km(latitude, longitude, report_latitude, report_longitude):
    """Great-circle distance; clamping avoids floating point errors at antipodes."""
    first, second = math.radians(latitude), math.radians(report_latitude)
    delta_lat = second - first
    delta_lon = math.radians(report_longitude - longitude)
    chord = (math.sin(delta_lat / 2) ** 2
             + math.cos(first) * math.cos(second) * math.sin(delta_lon / 2) ** 2)
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, chord))))


def nearby_reports(latitude, longitude, radius_km=10, limit=50, status=None):
    """Bound lookup geographically and in result size, including resolved history.

    An indexed bounding box selects candidates, then a great-circle distance
    filters and orders results in SQLite. Antimeridian and polar searches work
    without scanning unrelated reports into application memory.
    """
    if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (latitude, longitude, radius_km))
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
            or not 0 < radius_km <= 100):
        raise ValueError("Use finite coordinates and a radius greater than 0 and at most 100 km")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if status not in (None, "active", "resolved"):
        raise ValueError("status must be active or resolved")
    angular = radius_km / 6371.0088
    delta_lat = math.degrees(angular)
    south, north = max(-90, latitude - delta_lat), min(90, latitude + delta_lat)
    conditions = ["r.latitude BETWEEN ? AND ?"]
    parameters = [south, north]
    if south > -90 and north < 90:
        delta_lon = math.degrees(math.asin(min(1, math.sin(angular) / math.cos(math.radians(latitude)))))
        west, east = longitude - delta_lon, longitude + delta_lon
        if west < -180:
            conditions.append("(r.longitude >= ? OR r.longitude <= ?)")
            parameters.extend((west + 360, east))
        elif east > 180:
            conditions.append("(r.longitude >= ? OR r.longitude <= ?)")
            parameters.extend((west, east - 360))
        else:
            conditions.append("r.longitude BETWEEN ? AND ?")
            parameters.extend((west, east))
    if status:
        conditions.append("r.status = ?")
        parameters.append(status)
    conditions.append("report_distance(?, ?, r.latitude, r.longitude) <= ?")
    parameters.extend((latitude, longitude, radius_km))
    with database.connect() as connection:
        connection.create_function("report_distance", 4, _distance_km, deterministic=True)
        rows = connection.execute(f"""SELECT r.*,
            s.name AS original_site_name, s.latitude AS original_site_latitude,
            s.longitude AS original_site_longitude,
            report_distance(?, ?, r.latitude, r.longitude) AS distance_km
            FROM crew_reports r LEFT JOIN sites s ON s.id = r.site_id
            WHERE {' AND '.join(conditions)}
            ORDER BY distance_km, r.report_time DESC, r.id DESC LIMIT ?""",
            [latitude, longitude, *parameters, limit]).fetchall()
    reports = []
    for row in rows:
        item = dict(row)
        original_site = {"id": item["site_id"], "name": item.pop("original_site_name"),
                         "latitude": item.pop("original_site_latitude"),
                         "longitude": item.pop("original_site_longitude")}
        report = serialize_report(item)
        report["original_site"] = original_site
        report["provenance"] = {"source": "crew_report", "original_site_id": item["site_id"],
                                "report_id": item["id"], "report_time": item["report_time"]}
        reports.append(report)
    return reports


@router.get("/reports/nearby")
def get_nearby_reports(
    latitude: float = Query(ge=-90, le=90, allow_inf_nan=False),
    longitude: float = Query(ge=-180, le=180, allow_inf_nan=False),
    radius_km: float = Query(10, gt=0, le=100, allow_inf_nan=False),
    limit: int = Query(50, ge=1, le=100),
    status: str | None = Query(None, pattern=r"^(active|resolved)$"),
):
    """Find reported observations across sites, including resolved history by default."""
    rows = nearby_reports(latitude, longitude, radius_km, limit, status)
    return {"query": {"latitude": latitude, "longitude": longitude, "radius_km": radius_km,
                      "limit": limit, "status": status or "all"},
            "count": len(rows), "ordering": "distance_ascending_then_report_time_descending",
            "interpretation": "Crew observations are historical context, not verified present hazards or model probabilities.",
            "reports": rows}


def _validated_photo(data, content_type):
    """Decode and re-encode images; never serve arbitrary uploaded bytes or metadata."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if original.format != ACCEPTED_PHOTO_TYPES[content_type]:
                    raise HTTPException(status_code=415, detail="Photo encoding does not match Content-Type")
                if original.width * original.height > MAX_PHOTO_PIXELS:
                    raise HTTPException(status_code=413, detail="Photo exceeds 20 million pixels")
                original.verify()
            with Image.open(BytesIO(data)) as original:
                normalized = ImageOps.exif_transpose(original).convert("RGB")
                width, height = normalized.size
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=88)
                image_bytes = output.getvalue()
                if len(image_bytes) > MAX_PHOTO_BYTES:
                    raise HTTPException(status_code=413, detail="Processed photo exceeds 5 MiB; resize the image")
                return image_bytes, width, height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise HTTPException(status_code=413, detail="Photo dimensions are too large") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise HTTPException(status_code=422, detail="Could not decode a valid photo") from error


def _store_photo(site_id, data, content_type):
    image_bytes, width, height = _validated_photo(data, content_type)
    photo_id = uuid.uuid4().hex
    directory = upload_directory()
    directory.mkdir(parents=True, exist_ok=True)
    filename = photo_id + ".jpg"
    path = directory / filename
    path.write_bytes(image_bytes)
    created_at = utcnow()
    try:
        with database.connect() as connection:
            connection.execute("INSERT INTO crew_photos VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (photo_id, site_id, filename, "image/jpeg", len(image_bytes), width, height, created_at))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"photo_reference": photo_id, "site_id": site_id,
            "photo_url": f"/api/photos/{photo_id}", "media_type": "image/jpeg",
            "size_bytes": len(image_bytes), "width": width, "height": height, "created_at": created_at}


@router.post("/sites/{site_id}/photos", status_code=201)
async def upload_photo(site_id: int, request: Request):
    """Upload raw JPEG/PNG/WebP bytes with an image Content-Type (maximum 5 MiB)."""
    require_site(site_id)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ACCEPTED_PHOTO_TYPES:
        raise HTTPException(status_code=415, detail="Send raw image/jpeg, image/png, or image/webp bytes")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="Photo upload exceeds 5 MiB")
        body.extend(chunk)
    return await run_in_threadpool(_store_photo, site_id, bytes(body), content_type)


@router.get("/photos/{photo_id}")
def get_photo(photo_id: str):
    with database.connect() as connection:
        photo = connection.execute("SELECT * FROM crew_photos WHERE id = ?", (photo_id,)).fetchone()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = upload_directory() / photo["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stored photo is unavailable")
    return FileResponse(path, media_type=photo["media_type"],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


@router.post("/sites/{site_id}/reports", status_code=201)
def create_report(site_id: int, report: ReportCreate):
    require_site(site_id)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if report.photo_reference:
            photo = connection.execute("SELECT site_id FROM crew_photos WHERE id = ?", (report.photo_reference,)).fetchone()
            if photo is None or photo["site_id"] != site_id:
                raise HTTPException(status_code=422, detail="Photo reference must identify a photo uploaded to this site")
        cursor = connection.execute("""INSERT INTO crew_reports
            (site_id, hazard_type, description, latitude, longitude, photo_reference, report_time, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
            (site_id, report.hazard_type, report.description, report.latitude,
             report.longitude, report.photo_reference, utcnow()))
        row = connection.execute("SELECT * FROM crew_reports WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = serialize_report(row)
        notifications.publish(site_id, "crew_report_created", {"report": result},
                              f"crew-report:{result['id']}:created", connection=connection)
    return result


@router.get("/sites/{site_id}/reports")
def get_reports(site_id: int, status: str | None = Query(None, pattern=r"^(active|resolved)$")):
    """Omit status to include resolved observations in the site's location history."""
    require_site(site_id)
    return {"site_id": site_id, "reports": list_reports(site_id, status)}


@router.post("/sites/{site_id}/reports/{report_id}/resolve")
def resolve_report(site_id: int, report_id: int):
    require_site(site_id)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM crew_reports WHERE site_id = ? AND id = ?", (site_id, report_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Crew report not found for this site")
        if row["status"] == "resolved":
            return serialize_report(row)
        connection.execute("UPDATE crew_reports SET status = 'resolved', resolved_at = ? WHERE id = ?", (utcnow(), report_id))
        row = connection.execute("SELECT * FROM crew_reports WHERE id = ?", (report_id,)).fetchone()
        result = serialize_report(row)
        notifications.publish(site_id, "crew_report_resolved", {"report": result},
                              f"crew-report:{report_id}:resolved", connection=connection)
    return result
