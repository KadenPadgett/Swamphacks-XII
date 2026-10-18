"""Shape hourly model data into forecasts and completed rainfall windows."""

from datetime import datetime, timedelta


FORECAST_HOURS = 48
RAINFALL_WINDOWS = (24, 48, 72)
HOURLY_FIELDS = (
    "temperature_2m", "relative_humidity_2m", "precipitation_probability",
    "precipitation", "rain", "showers", "wind_speed_10m", "wind_gusts_10m",
    "apparent_temperature", "weather_code", "soil_moisture_3_to_9cm",
)


def request_params(latitude, longitude):
    return {
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,"
                   "wind_gusts_10m,apparent_temperature,weather_code,soil_moisture_3_to_9cm",
        "hourly": ",".join(HOURLY_FIELDS),
        "past_hours": max(RAINFALL_WINDOWS), "forecast_hours": FORECAST_HOURS + 1,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "precipitation_unit": "inch", "timezone": "auto",
    }


def weather_response(data, latitude, longitude):
    return {
        "source": "Open-Meteo",
        "requested_coordinates": {"latitude": latitude, "longitude": longitude},
        "timezone": data["timezone"], "utc_offset_seconds": data["utc_offset_seconds"],
        "current": data["current"], "units": data["current_units"],
        **hourly_and_rainfall(data),
    }


def hourly_and_rainfall(data):
    # Use the provider's time, not the backend machine's local clock.
    # All ISO timestamps share the provider's fixed utc_offset_seconds.
    end = datetime.fromisoformat(data["current"]["time"]).replace(
        minute=0, second=0, microsecond=0
    )
    hourly = data["hourly"]
    times = [datetime.fromisoformat(value) for value in hourly["time"]]
    if len(set(times)) != len(times) or times != sorted(times):
        raise ValueError("Hourly timestamps must be unique and ordered")
    for field in HOURLY_FIELDS:
        if not isinstance(hourly[field], list) or len(hourly[field]) != len(times):
            raise ValueError("Hourly arrays must match the timestamps")

    forecast_indices = [
        index for index, time in enumerate(times)
        if end < time <= end + timedelta(hours=FORECAST_HOURS)
    ]
    forecast = {
        field: [hourly[field][index] for index in forecast_indices]
        for field in ("time", *HOURLY_FIELDS)
    }

    rain_by_time = {}
    recent_indices = [index for index, time in enumerate(times)
                      if end - timedelta(hours=24) < time <= end]
    recent_hourly = {
        field: [hourly[field][index] for index in recent_indices]
        for field in ("time", "precipitation")
    }
    for index, time in enumerate(times):
        rain, showers = hourly["rain"][index], hourly["showers"][index]
        rain_by_time[time] = (
            rain + showers if rain is not None and showers is not None else None
        )

    windows = []
    for hours in RAINFALL_WINDOWS:
        # A sample at the window's start belongs to the preceding hour;
        # the sample at its end belongs in this window.
        amounts = [
            rain_by_time.get(end - timedelta(hours=offset))
            for offset in range(hours)
        ]
        available = sum(amount is not None for amount in amounts)
        windows.append({
            "hours": hours,
            "start": (end - timedelta(hours=hours)).isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"),
            "total": round(sum(amounts), 4) if available == hours else None,
            "available_hours": available,
            "complete": available == hours,
        })

    if data["hourly_units"]["rain"] != data["hourly_units"]["showers"]:
        raise ValueError("Rain and showers must use the same unit")

    return {
        "hourly": forecast,
        "recent_hourly": recent_hourly,
        "hourly_units": data["hourly_units"],
        "recent_rainfall": {
            "source_type": "weather_model",
            "components": ["rain", "showers"],
            "unit": data["hourly_units"]["rain"],
            "as_of": end.isoformat(timespec="minutes"),
            "windows": windows,
        },
    }
