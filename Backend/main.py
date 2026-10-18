from fastapi import FastAPI, HTTPException, Request
import asyncio
from fastapi.middleware.cors import CORSMiddleware
import httpx
from fastapi import Query
from pydantic import BaseModel, Field, ConfigDict, field_validator
import os
import secrets
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import database
import risk
import lightning
import refresh_jobs
import initial_assessments
import alerts
import notifications
import crew_reports
import briefings
import rivers
import hefs
import strike_feed
import ml_risk
import site_responses
from refresh_locks import RefreshBusy
from weather import request_params, weather_response
from risk_sources import utcnow

# Start up function. Initializes the database.
@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize_database()
    scheduler = refresh_jobs.RefreshScheduler()
    app.state.refresh_scheduler = scheduler
    if refresh_jobs.enabled():
        scheduler.start()
        retry_task = initial_assessments.queue_retries()
    else:
        retry_task = None
    try:
        yield
    finally:
        if retry_task:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
        await initial_assessments.stop()
        await scheduler.stop()


app = FastAPI(title="PlanIT Control Backend", version="0.3.0", lifespan=lifespan)
for router in (alerts.router, notifications.router, crew_reports.router, briefings.router, rivers.router, hefs.router, strike_feed.router):
    app.include_router(router)


@app.middleware("http")
async def protect_writes(request: Request, call_next):
    token = os.environ.get("PLANIT_WRITE_TOKEN")
    if token and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"detail": "A valid write token is required"}, status_code=401)
    return await call_next(request)

# Allow a locally running Vite frontend to call this backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys([
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        *[value.strip().rstrip('/') for value in os.environ.get("PLANIT_ALLOWED_ORIGINS", "").split(',') if value.strip()],
    ])),
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Authorization", "Last-Event-ID"],
)

# defines and validates the input.
# Name must contain a non-whitespace character, latitude/longitude
# are valid
class SiteCreate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    name: str = Field(min_length=1, pattern=r"\S")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            briefings._zone(value)
        return value

# defines response.
class Site(SiteCreate):
    id: int
    eia_plant_id: int | None = None
    utility: str | None = None
    county: str | None = None
    state: str | None = None
    plant_type: str | None = None
    source: str | None = None
    source_url: str | None = None
    source_period: str | None = None
    initial_assessment: dict | None = None

# Local machine will display whats in function if web url ends in what is in quotes
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/sites", response_model=list[Site])
def get_sites():
    return [{**site, "timezone": briefings.get_timezone(site["id"]),
             "initial_assessment": database.initial_assessment(site['id'])} for site in database.list_sites()]

# Third End Point. Front end will send a json with name, latitude,
# and longitude.
# The database assigns an ID and commits the site to disk.
@app.post("/api/sites", response_model=Site, status_code=201)
async def create_site(site: SiteCreate):
    result = database.insert_site(**site.model_dump(exclude={"timezone"}))
    if site.timezone:
        briefings.set_timezone(result["id"], site.timezone)
    initial_assessments.queue(result['id'])
    return {**result, "timezone": site.timezone,
            "initial_assessment": database.initial_assessment(result['id'])}

# gets site_id from the url itself and then uses that in function
def get_site(site_id: int):
    site = database.find_site(site_id)
    if site is None:
        raise HTTPException(
            status_code=404,
            detail="Site not found",
        )

    return site


@app.get("/api/sites/{site_id}", response_model=Site)
def get_site_record(site_id: int):
    return {**get_site(site_id), "timezone": briefings.get_timezone(site_id),
            "initial_assessment": database.initial_assessment(site_id)}
# API for weather data
def fetch_weather(latitude: float, longitude: float):
    url = "https://api.open-meteo.com/v1/forecast"

    # These become parameters in the request URL.
    # Specifies the location, requested measurements, and units
    params = request_params(latitude, longitude)
# Use parameters that were just made and call the meteo API
# with httpx. 
    try:
        response = httpx.get(url, params=params, timeout=10.0)
        # If http error raise it
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve weather from Open-Meteo.",
        ) from error
    try:
        data = response.json()
        return weather_response(data, latitude, longitude)
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            status_code=502,
            detail="Invalid weather data received from Open-Meteo.",
        ) from error
# First endpoint. Front end provides coordinates
# Query makes the coordinate, and ge an le ensure valid coordinates
@app.get("/api/weather")
def get_weather(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
):
    return fetch_weather(latitude, longitude)

# Second End point. Front End provides a saved sites ID

@app.get("/api/sites/{site_id}/weather")
def get_site_weather(site_id: int):
    site = get_site(site_id)

    weather = fetch_weather(
        latitude=site["latitude"],
        longitude=site["longitude"],
    )

    return {
        "site_id": site_id,
        "site_name": site["name"],
        "weather": weather,
    }


@app.get("/api/sites/{site_id}/risk")
async def get_site_risk(site_id: int):
    try:
        result = await risk.get_site_risk(get_site(site_id))
    except RefreshBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return site_responses.detail(database.find_site(site_id), result)


@app.get("/api/sites/{site_id}/detail")
def get_site_detail(site_id: int):
    """Saved combined view; never waits for a provider or for the optional model."""
    return site_responses.detail(get_site(site_id))


@app.post('/api/sites/{site_id}/assessment/retry')
async def retry_initial_assessment(site_id: int):
    get_site(site_id)
    if not database.retry_initial_assessment(site_id, utcnow().isoformat()):
        raise HTTPException(409, 'Only a failed initial assessment can be retried')
    initial_assessments.queue(site_id)
    return database.initial_assessment(site_id)


@app.get("/api/sites/{site_id}/ml-input")
async def get_model_input(site_id: int):
    try:
        assessment = await risk.get_site_risk(get_site(site_id))
    except RefreshBusy as error:
        raise HTTPException(409, str(error)) from error
    return ml_risk.request_for(assessment)


@app.post("/api/sites/{site_id}/ml-output")
def submit_model_output(site_id: int, output: ml_risk.ModelOutput):
    get_site(site_id)
    try:
        database.save_model_output(site_id, output.model_dump(mode="json"))
        return ml_risk.serialize_predictions(database.load_risk(site_id))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/sites/{site_id}/lightning")
def get_site_lightning(site_id: int):
    get_site(site_id)
    return lightning.saved(site_id)


@app.get("/api/refresh/status")
def get_refresh_status():
    return refresh_jobs.status()


@app.get("/api/risks")
def get_risks():
    """Fast portfolio view of saved assessments; pending/stale data is explicit."""
    return risk.risk_overview(database.list_sites())


@app.post("/api/risks/refresh")
async def refresh_risks(
    limit: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Refresh all monitored sites by default; optionally request a bounded page."""
    sites = database.list_sites()[offset:]
    if limit is not None:
        sites = sites[:limit]
    try:
        return await refresh_jobs.run("full", sites=sites, trigger="api")
    except RefreshBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
