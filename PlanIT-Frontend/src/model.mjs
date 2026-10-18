export const LEVELS = ["low", "watch", "high", "severe", "unknown", "info"];
export const LABELS = {
  low: "Low",
  watch: "Watch",
  high: "High",
  severe: "Severe",
  unknown: "Unknown",
  info: "Reported",
};
export const HAZARDS = {
  extreme_heat: {
    label: "Heat stress",
    short: "Heat",
    icon: "Thermometer",
    color: "#c47427",
  },
  lightning: {
    label: "Lightning",
    short: "Lightning",
    icon: "Zap",
    color: "#96763a",
  },
  flooding: {
    label: "Flooding",
    short: "Flood",
    icon: "Waves",
    color: "#4f8c9e",
  },
  soil_saturation: {
    label: "Ground conditions",
    short: "Ground",
    icon: "Layers",
    color: "#887354",
  },
  weather: {
    label: "Wind & weather",
    short: "Weather",
    icon: "Wind",
    color: "#728c80",
  },
  wildfire: {
    label: "Wildfire",
    short: "Fire",
    icon: "Flame",
    color: "#c66c4d",
  },
  wildlife_sightings: {
    label: "Wildlife sightings",
    short: "Wildlife",
    icon: "Binoculars",
    color: "#70805a",
  },
};
export const num = (value) =>
  typeof value === "number" && Number.isFinite(value);
export const title = (str) =>
  (str || "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
export const level = (value) => (LEVELS.includes(value) ? value : "unknown");
export function normalizeRow(row) {
  const s = row.site || row;
  return {
    ...row,
    site: { ...s, id: s.id ?? row.site_id },
    site_id: s.id ?? row.site_id,
    rule_based_severity: level(
      row.rule_based_severity || row.current_risk?.level,
    ),
    current_score:
      num(row.current_score) &&
      !row.ml_assessment?.stale &&
      ["available", "partial"].includes(row.ml_availability)
        ? row.current_score
        : null,
    active_alert_status: row.active_alert_status || {
      count: 0,
      stop_work: false,
    },
    required_safety_actions: row.required_safety_actions || [],
    stale: !!row.stale,
    rank: row.rank ?? 999,
  };
}
export function sortRows(rows) {
  return rows.map(normalizeRow).sort((a, b) => a.rank - b.rank);
}
export function scoreOf(detail) {
  const m = detail?.ml_predictions || {};
  return !m.stale &&
    ["available", "partial"].includes(m.availability) &&
    (m.current_availability == null ||
      m.current_availability === "available") &&
    num(m.current_score)
    ? m.current_score
    : null;
}
export function initialAssessmentLabel(initial, hasSnapshot = false) {
  if (!initial) return hasSnapshot ? "Assessed" : "Assessment unavailable";
  if (initial.status === "pending" || initial.status === "running") return "Assessing…";
  if (initial.status === "failed") return "Assessment failed";
  return "Assessed";
}
export function weatherModelView(model, metar, now = Date.now()) {
  const expired = model?.availability === "available" && model.valid_until &&
    Number.isFinite(Date.parse(model.valid_until)) && now >= Date.parse(model.valid_until);
  const status = expired ? "stale" : model?.availability || "pending";
  const reason = model?.reason || "";
  const inputStatus = metar?.availability || model?.input_availability || "pending";
  let label = "Prediction pending";
  if (status === "stale" || reason === "prediction_expired") label = "Prediction expired";
  else if (status === "unavailable")
    label = reason.startsWith("metar_") ? "Missing METAR input" : "Prediction unavailable";
  else if (status === "available") label = "Experimental estimate";
  return {
    status,
    label,
    probability: status === "available" && num(model?.probability) ? model.probability : null,
    inputStatus,
    inputReason: metar?.reason || model?.input_reason || null,
    reason: reason || null,
  };
}
export function countdownState(feed, clock) {
  if (!feed)
    return { status: "unknown", seconds: 0, label: "Coverage unavailable" };
  const now = clock instanceof Date ? clock.getTime() : Number(clock);
  const deadline = Date.parse(feed.deadline);
  const seconds = Number.isFinite(deadline)
    ? Math.max(0, Math.ceil((deadline - now) / 1000))
    : 0;
  if (seconds > 0)
    return { status: "stop_work", seconds, label: "Outdoor work paused" };
  const end = Date.parse(
    feed.last_success_at ||
      feed.provider_verification?.last_successful_coverage_at,
  );
  const fresh = Number.isFinite(end) && now >= end && now - end <= 90000;
  if (
    feed.status === "clear" &&
    feed.coverage_complete &&
    feed.availability === "available" &&
    fresh
  )
    return { status: "clear", seconds: 0, label: "Measured window clear" };
  return {
    status: "unknown",
    seconds: 0,
    label:
      feed.status === "stop_work"
        ? "Hold elapsed · awaiting fresh coverage"
        : "Awaiting complete coverage",
  };
}
export function hazardMetric(key, card) {
  const e = card?.evidence || {};
  if (key === "extreme_heat")
    return {
      value: num(e.apparent_temperature_f)
        ? Math.round(e.apparent_temperature_f)
        : "—",
      unit: "°F",
      label: "Feels like",
    };
  if (key === "flooding")
    return {
      value: num(e.preceding_24h_precipitation_in)
        ? e.preceding_24h_precipitation_in.toFixed(1)
        : "—",
      unit: "in",
      label: "Rain · past 24 hours",
    };
  if (key === "soil_saturation")
    return {
      value: num(e.volumetric_water_content)
        ? e.volumetric_water_content.toFixed(2)
        : "—",
      unit: "m³/m³",
      label: "Modeled soil moisture",
    };
  if (key === "weather")
    return {
      value: num(e.gust_mph) ? Math.round(e.gust_mph) : "—",
      unit: "mph",
      label: "Wind gusts",
    };
  if (key === "lightning")
    return {
      value:
        card?.measured_lightning_strikes?.availability === "available" &&
        num(card.measured_lightning_strikes.count)
          ? card.measured_lightning_strikes.count
          : "—",
      unit: "",
      label: "Measured strikes · 30 min",
    };
  if (key === "wildlife_sightings")
    return {
      value: num(e.total_results) ? e.total_results : "—",
      unit: "",
      label: "Contextual observations",
    };
  return {
    value: Array.isArray(e.nearby_events) ? e.nearby_events.length : "—",
    unit: "",
    label: "Nearby reported fires",
  };
}
export function hourLabel(value, tz = "America/New_York") {
  try {
    return new Date(value).toLocaleTimeString("en-US", {
      hour: "numeric",
      timeZone: tz,
    });
  } catch {
    return "—";
  }
}
export function dateLabel(value, tz = "America/New_York", withTime = true) {
  if (!value) return "Not yet available";
  try {
    return new Date(value).toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      ...(withTime ? { hour: "numeric", minute: "2-digit" } : {}),
      timeZone: tz,
    });
  } catch {
    return value;
  }
}
export function ageLabel(value, now = Date.now()) {
  if (!value) return "Not assessed";
  const m = Math.max(0, Math.floor((now - Date.parse(value)) / 60000));
  return m < 1
    ? "Just now"
    : m < 60
      ? `${m}m ago`
      : m < 1440
        ? `${Math.floor(m / 60)}h ago`
        : `${Math.floor(m / 1440)}d ago`;
}
export function flattenEvidence(card) {
  return Object.entries(card?.evidence || {})
    .filter(
      ([, v]) =>
        v != null &&
        (typeof v === "number" ||
          typeof v === "string" ||
          typeof v === "boolean"),
    )
    .map(([k, v]) => [
      title(k),
      typeof v === "boolean" ? (v ? "Yes" : "No") : String(v),
    ]);
}
export function boundText(row) {
  const b = row?.probability_bounds;
  const lo = Array.isArray(b) ? b[0] : b?.lower,
    hi = Array.isArray(b) ? b[1] : b?.upper;
  if (!num(lo) || !num(hi)) return "Unavailable";
  return lo === 0
    ? `At most ${(hi * 100).toFixed(0)}%`
    : `${(lo * 100).toFixed(0)}–${(hi * 100).toFixed(0)}%`;
}
