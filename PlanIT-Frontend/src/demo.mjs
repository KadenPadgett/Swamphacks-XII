const anchor = new Date();
anchor.setHours(6, 45, 0, 0);
export const DEMO_NOW = anchor.getTime();
const iso = (offset) => new Date(DEMO_NOW + offset * 60000).toISOString();
const SITES = [
  [
    1,
    "Everglades transmission",
    "South access road",
    25.75,
    -80.7,
    "high",
    2,
    103,
    0.45,
    2.4,
  ],
  [
    2,
    "Big Cypress mobilization",
    "West staging area",
    25.99,
    -81.14,
    "high",
    1,
    99,
    0.44,
    2.1,
  ],
  [
    3,
    "North levee expansion",
    "Canal works",
    26.26,
    -80.77,
    "watch",
    0,
    96,
    0.33,
    1.2,
  ],
  [
    4,
    "Homestead substation",
    "Equipment yard",
    25.45,
    -80.48,
    "low",
    0,
    86,
    0.22,
    0.3,
  ],
  [
    5,
    "Biscayne corridor",
    "Eastern connection",
    25.52,
    -80.21,
    "low",
    0,
    88,
    0.24,
    0.5,
  ],
  [
    6,
    "Pinecrest distribution",
    "Line maintenance",
    25.66,
    -80.31,
    "unknown",
    0,
    null,
    null,
    null,
  ],
];
const action =
  "Suspend work in threatened low areas; move crews and equipment to higher ground and review evacuation routes.";
function indicators(temp, soil, rain, sev) {
  return {
    extreme_heat: {
      level:
        temp == null
          ? "unknown"
          : temp >= 110
            ? "severe"
            : temp >= 100
              ? "high"
              : temp >= 90
                ? "watch"
                : "low",
      coverage: temp == null ? "unavailable" : "available",
      basis: "Apparent-temperature rule at this time",
      evidence: { apparent_temperature_f: temp, thresholds_f: [90, 100, 110] },
      sources: ["weather"],
    },
    flooding: {
      level:
        rain == null
          ? "unknown"
          : rain >= 2
            ? "high"
            : rain >= 1
              ? "watch"
              : "low",
      coverage: rain == null ? "unavailable" : "available",
      basis: "Preceding 24-hour rainfall screen; no inundation model",
      evidence: {
        preceding_24h_precipitation_in: rain,
        rain_thresholds_in: [1, 2, 4],
      },
      sources: ["weather", "alerts"],
    },
    soil_saturation: {
      level:
        soil == null
          ? "unknown"
          : soil >= 0.4
            ? "high"
            : soil >= 0.3
              ? "watch"
              : "low",
      coverage: soil == null ? "unavailable" : "available",
      basis: "Modeled soil wetness; bearing capacity requires a site check",
      evidence: {
        volumetric_water_content: soil,
        depth_cm: [3, 9],
        saturation_percent: null,
      },
      sources: ["weather"],
    },
    lightning: {
      level: "unknown",
      coverage: "partial",
      basis: "Thunderstorm outlook and measured coverage are separate",
      evidence: { weather_code: 2 },
      sources: ["weather", "alerts"],
      thunderstorm_forecast: { availability: "available", level: "low" },
      measured_lightning_strikes: { availability: "unknown", count: null },
    },
    weather: {
      level: "low",
      coverage: "available",
      basis: "Wind gusts and rainfall at this time",
      evidence: { gust_mph: 12, preceding_hour_precipitation_in: 0 },
      sources: ["weather"],
    },
    wildfire: {
      level: "low",
      coverage: "available",
      basis: "Reported wildfire points and applicable fire alerts",
      evidence: { nearby_events: [], search_radius_km: 50 },
      sources: ["wildfires"],
    },
    wildlife_sightings: {
      level: "info",
      coverage: "available",
      basis: "Observations provide context, not a danger rating",
      evidence: { total_results: 4 },
      sources: ["wildlife"],
    },
  };
}
export function seedDemo() {
  const details = {};
  const sites = [];
  SITES.forEach(
    (
      [id, name, area, latitude, longitude, sev, alertsCount, temp, soil, rain],
      i,
    ) => {
      const site = {
        id,
        name,
        latitude,
        longitude,
        state: "FL",
        county: "Miami-Dade",
        utility: area,
      };
      const cards = indicators(temp, soil, rain, sev);
      if (sev === "low") {
        cards.lightning = {
          ...cards.lightning,
          level: "low",
          coverage: "available",
          measured_lightning_strikes: { availability: "available", count: 0 },
        };
      }
      const alerts = alertsCount
        ? [
            {
              id: id * 10,
              site_id: id,
              hazard: "flooding",
              severity: "high",
              status: "active",
              stop_work: true,
              message: "Flooding: elevated rainfall at the site",
              required_action: action,
              created_at: iso(-18),
              updated_at: iso(-2),
              origin: "rule_based",
              evidence: { preceding_24h_precipitation_in: rain },
            },
          ]
        : [];
      if (alertsCount === 2)
        alerts.push({
          id: id * 10 + 1,
          site_id: id,
          hazard: "extreme_heat",
          severity: "high",
          status: "active",
          stop_work: false,
          message: "Heat stress: feels-like temperature above 100°F",
          required_action:
            "Apply the site heat plan: shade, water, rest breaks, and supervisor review before exposed work continues.",
          created_at: iso(-12),
          updated_at: iso(-2),
          origin: "rule_based",
          evidence: { apparent_temperature_f: temp },
        });
      const hourly = Array.from({ length: 12 }, (_, h) => {
        const t =
          temp == null
            ? null
            : Math.round(temp + [0, 2, 4, 6, 8, 9, 7, 5, 2, -1, -4, -6][h]);
        const l =
          t == null
            ? "unknown"
            : t >= 110
              ? "severe"
              : t >= 100
                ? "high"
                : t >= 90
                  ? "watch"
                  : "low";
        return {
          time: iso(15 + h * 60),
          overall: { level: l, highest_known_level: l },
          indicators: { ...indicators(t, soil, rain, l) },
        };
      });
      const countdown = {
        site_id: id,
        status: "unknown",
        availability: "unknown",
        coverage_complete: false,
        deadline: null,
        latest_strike_at: null,
        last_success_at: null,
        server_time: iso(0),
        provider_verification: {
          provider: "xweather",
          feed_access: "not_verified",
          strike_detection: "not_yet_observed",
        },
      };
      if (sev === "low")
        Object.assign(countdown, {
          status: "clear",
          availability: "available",
          coverage_complete: true,
          last_success_at: iso(0),
          coverage_start: iso(-35),
          provider_verification: {
            provider: "xweather",
            feed_access: "verified_current",
            strike_detection: "not_yet_observed",
          },
        });
      const current = {
        time: iso(-2),
        overall: {
          level: sev,
          highest_known_level: sev,
          coverage_complete: sev === "low",
        },
        indicators: cards,
      };
      const reports =
        id === 1
          ? [
              {
                id: 1,
                site_id: id,
                hazard_type: "wildlife",
                description:
                  "Alligator observed beside the south access road. Keep the crew clear of the waterline.",
                latitude: 25.748,
                longitude: -80.694,
                report_time: iso(-35),
                status: "active",
                photo_url: null,
              },
              {
                id: 2,
                site_id: id,
                hazard_type: "soil_saturation",
                description:
                  "Access road is soft after overnight rain. Equipment is holding at the staging area.",
                latitude: 25.754,
                longitude: -80.705,
                report_time: iso(-20),
                status: "active",
                photo_url: null,
              },
            ]
          : id === 2
            ? [
                {
                  id: 3,
                  site_id: id,
                  hazard_type: "flooding",
                  description:
                    "Standing water along the west access track. Supervisor review requested.",
                  latitude,
                  longitude,
                  report_time: iso(-50),
                  status: "active",
                },
              ]
            : [];
      const d = {
        site,
        status: "ready",
        generated_at: id === 6 ? iso(-55) : iso(-2),
        rule_based_assessment: {
          assessment_type: "rule_based",
          current,
          hourly,
          highest_upcoming: hourly[5],
        },
        hazard_evidence: cards,
        conditions: {
          timezone: "America/New_York",
          current: {
            time: iso(-2),
            apparent_temperature: temp,
            wind_gusts_10m: 12,
            soil_moisture_3_to_9cm: soil,
          },
          hourly: {
            time: hourly.map((x) => x.time),
            apparent_temperature: hourly.map(
              (x) => x.indicators.extreme_heat.evidence.apparent_temperature_f,
            ),
          },
        },
        ml_predictions: {
          availability: "unavailable",
          current_score: null,
          stale: false,
          reason: "trained_model_not_configured",
          hourly_scores: [],
        },
        required_safety_actions: alerts.map((a) => ({
          alert_id: a.id,
          hazard: a.hazard,
          action: a.required_action,
          stop_work: a.stop_work,
          basis: "rule_based",
        })),
        alerts,
        active_crew_reports: reports,
        timezone: { timezone: "America/New_York", availability: "available" },
        lightning_countdown: countdown,
        freshness: {
          stale: id === 6,
          assessment_generated_at: iso(-2),
          weather_observed_at: iso(-3),
          expires_at: id === 6 ? iso(-40) : iso(13),
          coverage_complete: sev === "low",
          sources: {
            weather: {
              status: id === 6 ? "unavailable" : "available",
              retrieved_at: iso(-3),
            },
            alerts: { status: "available", retrieved_at: iso(-2) },
            wildlife: { status: "available", retrieved_at: iso(-8) },
          },
        },
        river_forecast: {
          availability: "unavailable",
          reason: "No validated forecast association",
          probability: null,
          individual_times: [],
          period_maxima: [],
          applicability: "context_only",
        },
        river_exposure: {
          site_relevance_verified: false,
          status: "unconfirmed",
        },
        notifications_url: `/api/sites/${id}/events`,
      };
      details[id] = d;
      sites.push({
        site,
        site_id: id,
        rank: i + 1,
        rule_based_severity: sev,
        current_score: null,
        ml_availability: "unavailable",
        active_alert_status: {
          count: alerts.length,
          stop_work: alerts.some((a) => a.stop_work),
          highest_severity: alerts.length ? "high" : "unknown",
        },
        required_safety_actions: d.required_safety_actions,
        coverage_status: sev === "low" ? "complete" : "incomplete",
        assessment_status: id === 6 ? "stale" : "current",
        stale: id === 6,
        generated_at: d.generated_at,
        lightning_countdown: countdown,
        current_risk: current.overall,
      });
    },
  );
  return { sites, details, briefings: {} };
}
