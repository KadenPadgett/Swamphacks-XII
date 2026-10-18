"""Read-only public NWPS verification; no site exposure is inferred or saved.

Run: python scripts/probe_nwps.py --gauge MEMT1 --catalogue
This makes one schema request, one detail request and optionally one catalogue
request. Do not repeatedly invoke it: the live catalogue advertises 10 requests
per five minutes. Normal application discovery shares a cached daily catalogue.
"""
import argparse
import json
import re

import httpx


BASE = "https://api.water.noaa.gov/nwps/v1"


def fetch(client, path, params=None):
    try:
        response = client.get(BASE + path, params=params)
        if response.status_code != 200:
            return {"http_status": response.status_code, "retry_after": response.headers.get("retry-after")}, None
        return {"http_status": response.status_code}, response.json()
    except httpx.TimeoutException:
        return {"error": "timeout"}, None
    except httpx.RequestError:
        return {"error": "network_error"}, None
    except ValueError:
        return {"error": "invalid_json"}, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gauge", default="MEMT1")
    parser.add_argument("--catalogue", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9]{3,20}", args.gauge):
        parser.error("gauge must be a NOAA identifier containing 3-20 letters or digits")
    results = {}
    with httpx.Client(timeout=12) as client:
        meta, schema = fetch(client, "/docs/swagger.json")
        if schema:
            parameters = schema.get("paths", {}).get("/nwps/v1/gauges", {}).get("get", {}).get("parameters", [])
            meta["query_parameters"] = [{"name": row["name"], "required": row.get("required"),
                                         "enum": row.get("enum")} for row in parameters]
        results["schema"] = meta
        if args.catalogue:
            meta, data = fetch(client, "/gauges", {"srid": "EPSG_4326"})
            if data:
                meta["gauge_count"] = len(data.get("gauges", []))
                meta["sample_fields"] = sorted((data.get("gauges") or [{}])[0])
            results["catalogue"] = meta
        meta, data = fetch(client, "/gauges/" + args.gauge)
        if data:
            meta.update({key: data.get(key) for key in ("lid", "name", "latitude", "longitude", "status")})
            flood = data.get("flood", {})
            meta["thresholds"] = flood.get("categories")
            meta["threshold_units"] = {"stage": flood.get("stageUnits"), "flow": flood.get("flowUnits")}
        results["gauge"] = meta
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
