"""Independent fast thunderstorm monitor. No strike measurements are fabricated."""

from datetime import timedelta

import database
import risk
import alerts
import strike_feed
from risk_sources import parse_time, utcnow


async def refresh_site(site, sources):
    feeds = await sources.for_lightning(site)
    now = utcnow()
    weather = feeds["weather"].get("data") or {}
    # Only the lightning card is retained; this path never fetches wildlife/fire feeds.
    frame = risk.assess_frame(site, feeds, weather.get("current", {}), now, None, None, predicted=True)
    card = frame["indicators"]["lightning"]
    card["thunderstorm_forecast"]["time_scope"] = "current_model_estimate"
    payload = {
        "site_id": site["id"], "checked_at": now.isoformat(),
        "weather_observed_at": weather.get("current", {}).get("time"),
        "utc_offset_seconds": weather.get("utc_offset_seconds"),
        "indicator": card,
        "sources": {name: {key: value for key, value in feed.items() if key != "data"}
                    for name, feed in feeds.items()},
        "measured_strike_feed_configured": False,
    }
    database.save_lightning(site["id"], payload)
    alerts.evaluate({"site": site, "generated_at": now.isoformat(),
                     "current_risk": {"indicators": {"lightning": card}}, "sources": payload["sources"]})
    return payload


def saved(site_id, max_age_seconds=90):
    payload = database.load_lightning(site_id)
    if payload is None:
        return {"site_id": site_id, "status": "pending", "stale": True,
                "measured_strike_feed_configured": strike_feed.configured(), "countdown": strike_feed.state(site_id)}
    return {**payload, "status": "ready", "countdown": strike_feed.state(site_id),
            "measured_strike_feed_configured": strike_feed.configured(),
            "stale": utcnow() - parse_time(payload["checked_at"]) > timedelta(seconds=max_age_seconds)}
