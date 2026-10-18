import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  sortRows,
  scoreOf,
  countdownState,
  hazardMetric,
  boundText,
  initialAssessmentLabel,
  weatherModelView,
} from "../src/model.mjs";
const example = JSON.parse(
  readFileSync(new URL("fixtures/backend-dangerous.json", import.meta.url)),
);
const missing = JSON.parse(
  readFileSync(new URL("fixtures/backend-unavailable.json", import.meta.url)),
);
test("actual backend fixture retains null model scores and backend priority", () => {
  const rows = sortRows(example.map_response.sites);
  assert.equal(rows[0].current_score, null);
  assert.equal(rows[0].rank, example.map_response.sites[0].rank);
  assert.equal(scoreOf(example.detail_response), null);
});
test("a genuine zero is preserved; stale/expired model output is not displayed", () => {
  const d = {
    ml_predictions: {
      availability: "available",
      current_availability: "available",
      current_score: 0,
      stale: false,
    },
  };
  assert.equal(scoreOf(d), 0);
  d.ml_predictions.stale = true;
  assert.equal(scoreOf(d), null);
  d.ml_predictions.stale = false;
  d.ml_predictions.current_availability = "stale";
  assert.equal(scoreOf(d), null);
});
test("elapsed lightning timer never creates an all-clear", () => {
  const now = Date.parse("2026-10-17T12:00:00Z");
  assert.equal(
    countdownState(
      {
        status: "stop_work",
        deadline: "2026-10-17T11:59:00Z",
        coverage_complete: false,
      },
      now,
    ).status,
    "unknown",
  );
  assert.equal(
    countdownState(
      { status: "stop_work", deadline: "2026-10-17T12:15:00Z" },
      now,
    ).seconds,
    900,
  );
  assert.equal(
    countdownState(
      {
        status: "clear",
        coverage_complete: true,
        availability: "available",
        last_success_at: "2026-10-17T11:59:50Z",
      },
      now,
    ).status,
    "clear",
  );
  assert.equal(
    countdownState(
      {
        status: "clear",
        coverage_complete: true,
        availability: "available",
        last_success_at: "2026-10-17T11:50:00Z",
      },
      now,
    ).status,
    "unknown",
  );
});
test("flooding shows measured rainfall, not a probability", () => {
  const m = hazardMetric(
    "flooding",
    example.detail_response.hazard_evidence.flooding,
  );
  assert.equal(m.unit, "in");
  assert.equal(m.value, "0.0");
  assert.equal(m.label, "Rain · past 24 hours");
});
test("unknown provider values remain unavailable instead of becoming zero", () => {
  assert.equal(hazardMetric("extreme_heat", {}).value, "—");
  assert.equal(
    hazardMetric("soil_saturation", { evidence: { saturation_percent: 99 } })
      .value,
    "—",
  );
  assert.equal(scoreOf(missing.detail_response), null);
});
test("HEFS bounds retain upper-bound semantics", () => {
  assert.equal(boundText({ probability_bounds: [0, 0.05] }), "At most 5%");
  assert.equal(boundText({ probability_bounds: [0.05, 0.1] }), "5–10%");
  assert.equal(boundText({ probability: null }), "Unavailable");
});
test("initial assessment states and experimental warning output stay separate from ranking", () => {
  assert.equal(initialAssessmentLabel({ status: "pending" }), "Assessing…");
  assert.equal(initialAssessmentLabel({ status: "running" }), "Assessing…");
  assert.equal(initialAssessmentLabel({ status: "failed" }), "Assessment failed");
  assert.equal(initialAssessmentLabel({ status: "completed" }), "Assessed");
  const row = sortRows([{ site: { id: 5 }, rank: 1, rule_based_severity: "high",
    ml_availability: "unavailable", current_score: null,
    weather_model: { availability: "available", probability: .8, score: 8 } }])[0];
  assert.equal(row.rule_based_severity, "high");
  assert.equal(row.current_score, null);
  assert.equal(weatherModelView(row.weather_model, { availability: "stale", reason: "old_observation" }).probability, .8);
  assert.equal(weatherModelView(row.weather_model, { availability: "stale" }).inputStatus, "stale");
  assert.equal(weatherModelView({ availability: "unavailable", reason: "metar_no_recent_observation" }, {}).label, "Missing METAR input");
  assert.equal(weatherModelView({ availability: "pending" }, {}).label, "Prediction pending");
  assert.equal(weatherModelView({ availability: "stale", reason: "prediction_expired", probability: .8 }, {}).probability, null);
  assert.equal(weatherModelView({ availability: "available", probability: .8,
    valid_until: "2026-10-17T12:00:00Z" }, {}, Date.parse("2026-10-17T12:00:01Z")).probability, null);
});
