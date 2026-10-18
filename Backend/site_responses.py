"""Frontend contracts keep rules, required actions, and actual model output separate."""
import alerts
import briefings
import crew_reports
import database
import lightning
import ml_risk
import metar_model
import risk
import strike_feed
import freshness
import rivers

RANKING = {
    "type": "rule_based", "version": "stop-work-then-coverage-v2",
    "description": "Stop-work first; then known severe/high/watch hazards; then incomplete, missing or stale assessments; then complete low. Site ID breaks ties.",
    "ml_scores_used": False, "missing_ml": "Scores remain null and do not affect ordering",
}


def model_for(snapshot, now=None):
    return ml_risk.serialize_predictions(snapshot, now=now)


def detail(site, snapshot=None):
    import hefs
    snapshot = snapshot if snapshot is not None else database.load_risk(site["id"])
    model = model_for(snapshot)
    initial = database.initial_assessment(site['id'])
    return {
        **(snapshot or {}), "site": site, "status": "ready" if snapshot else "pending",
        "initial_assessment": initial,
        "rule_based_assessment": {"assessment_type": "rule_based", "current": (snapshot or {}).get("current_risk"),
                                  "hourly": (snapshot or {}).get("hourly_predicted_risk", []),
                                  "highest_upcoming": (snapshot or {}).get("highest_upcoming_risk")},
        "conditions": (snapshot or {}).get("weather"),
        "hazard_evidence": (snapshot or {}).get("indicators", {}),
        "ml_predictions": model, "ml_assessment": model,
        "metar": (snapshot or {}).get('metar', {"availability": "pending", "reason": "assessment_pending"}),
        "weather_model": metar_model.serialize((snapshot or {}).get('weather_model')),
        "required_safety_actions": alerts.required_actions(site["id"]),
        "alerts": alerts.for_site(site["id"]), "active_crew_reports": crew_reports.active_reports(site["id"]),
        "timezone": briefings.timezone_status(site["id"]),
        "lightning_monitor": lightning.saved(site["id"]), "lightning_countdown": strike_feed.state(site["id"]),
        "freshness": {**freshness.assessment(snapshot), 'stale': not risk.snapshot_is_fresh(snapshot, site)},
        "river_forecast": hefs.saved(site['id']),
        "river_exposure": rivers.exposure_for(site['id']),
        "notifications_url": f"/api/sites/{site['id']}/events",
    }


def overview(sites):
    import hefs
    rows = []
    for site in sites:
        initial = database.initial_assessment(site['id'])
        snapshot = database.load_risk(site["id"])
        if snapshot and (snapshot.get("method_version") != risk.METHOD_VERSION
                         or snapshot.get("assessment_revision") != risk.ASSESSMENT_REVISION):
            snapshot = None
        current = (snapshot or {}).get("current_risk", {}).get("overall")
        peak = (snapshot or {}).get("highest_upcoming_risk")
        model = model_for(snapshot)
        age = freshness.assessment(snapshot)
        site_alerts = alerts.for_site(site["id"])
        known_level = (current or {}).get("highest_known_level", "unknown")
        incomplete = not (current or {}).get('coverage_complete', False)
        uncertain = incomplete or age['stale'] or (current or {}).get('level') == 'unknown'
        current_level = known_level if alerts.RANK.get(known_level, -1) >= 1 else 'unknown' if uncertain else 'low'
        alert_level = max((a["severity"] for a in site_alerts), key=lambda v: alerts.RANK[v], default="unknown")
        level = max((current_level, alert_level), key=lambda v: alerts.RANK[v])
        if alerts.RANK[level] <= 0 and uncertain:
            level = 'unknown'
        unavailable = model.get("availability") not in ("available", "partial") or model.get("stale", False)
        rows.append({
            "site": site, "site_id": site["id"], "latitude": site["latitude"], "longitude": site["longitude"],
            "status": "ready" if snapshot else "pending", "current_score": None if unavailable else model.get("current_score"),
            "initial_assessment": initial,
            "ml_availability": "stale" if model.get("stale") else model.get("availability", "unavailable"),
            "upcoming_peak": {"rule_based": peak, "ml_prediction": None if unavailable else model.get("highest_upcoming")},
            "current_risk": current, "overall": current,
            "highest_upcoming_risk": ({"time": peak["time"], **peak["overall"]} if peak else None),
            "ml_assessment": {k: v for k, v in model.items() if k != "hourly_scores"},
            "metar": (snapshot or {}).get('metar', {"availability": "pending", "reason": "assessment_pending"}),
            "weather_model": metar_model.serialize((snapshot or {}).get('weather_model')),
            "active_alert_status": {"count": len(site_alerts), "stop_work": any(a["stop_work"] for a in site_alerts),
                                    "highest_severity": alert_level},
            "rule_based_severity": level, "ranking_basis": "rule_based",
            "coverage_status": 'incomplete' if incomplete else 'complete',
            "assessment_status": 'missing' if snapshot is None else 'stale' if age['stale'] else 'current',
            "ranking_reason": 'active_stop_work' if any(a['stop_work'] for a in site_alerts)
                else 'known_elevated_hazard' if alerts.RANK[level] >= 1 else 'assessment_uncertain' if uncertain else 'complete_low',
            "required_safety_actions": alerts.required_actions(site["id"]),
            "stale": not risk.snapshot_is_fresh(snapshot, site), "generated_at": (snapshot or {}).get("generated_at"),
            "sources": (snapshot or {}).get("sources", {}), "lightning_countdown": strike_feed.state(site["id"]),
            "lightning_monitor": lightning.saved(site["id"]), "risk_url": f"/api/sites/{site['id']}/risk",
            "freshness": age, "river_forecast": hefs.saved(site['id']),
            "river_exposure": rivers.exposure_for(site['id']),
        })
    ranks = {"severe": 4, "high": 3, "watch": 2, "unknown": 1, "info": 1, "low": 0}
    rows.sort(key=lambda r: (-int(r["active_alert_status"]["stop_work"]), -ranks[r["rule_based_severity"]], r["site_id"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return {"count": len(rows), "ranking": RANKING, "sites": rows}
