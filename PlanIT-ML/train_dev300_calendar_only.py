"""PlanIT 2025 research ablation: calendar-only versus existing weather model.

Only input features: hour_sin, hour_cos, month_sin, month_cos.
Uses the EXACT chronological boundaries, train-only random row sampling procedure,
and HistGradientBoosting hyperparameters of train_dev300_baseline.py.
Validation chooses a per-model F1 threshold; test is never used to tune it.

Run from risk_assessment:
    py -3.13 train_dev300_calendar_only.py

Outputs (separate from original weather-model artifacts):
    data/processed/development_300_2025/model_baseline/calendar_only/
      calendar_hgb.joblib
      calendar_metrics.json
      comparison_with_weather.json
      comparison_with_weather.txt

Then run the existing event diagnostic, with separate arguments:
    py -3.13 diagnose_dev300_baseline.py ^
      --model data/processed/development_300_2025/model_baseline/calendar_only/calendar_hgb.joblib ^
      --metrics data/processed/development_300_2025/model_baseline/calendar_only/calendar_metrics.json ^
      --out data/processed/development_300_2025/model_baseline/calendar_only/diagnostics

In PowerShell, use one single line instead of the Windows cmd.exe ^ continuations.
Research only: not a meteorological forecast or a field-work safety indicator.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DEV = ROOT / "data/processed/development_300_2025"
DATA = DEV / "dev300_hourly_training_2025.csv.gz"
WEATHER_METRICS = DEV / "model_baseline/baseline_metrics.json"
OUT = DEV / "model_baseline/calendar_only"
TARGET = "label_new_sv_warning_next_6h"
TIME = "prediction_time_utc"
FEATURES = ["hour_sin", "hour_cos", "month_sin", "month_cos"]
# Exclusivity of upper bounds gives the same six-hour embargo as the original trainer.
BOUNDARIES = [
    ("train", "2025-01-01T00:00Z", "2025-08-31T18:00Z"),
    ("validation", "2025-09-01T00:00Z", "2025-10-31T18:00Z"),
    ("test", "2025-11-01T00:00Z", "2025-12-31T18:00Z"),
]


def read_splits(input_file: Path, keep: float, seed: int, chunksize: int):
    rng = np.random.default_rng(seed)
    frames = {name: [] for name, _, _ in BOUNDARIES}
    boundaries = [(n, pd.Timestamp(a), pd.Timestamp(b)) for n, a, b in BOUNDARIES]
    n_scanned = 0
    for chunk in pd.read_csv(input_file, usecols=[*FEATURES, TARGET, TIME],
                             chunksize=chunksize, low_memory=False):
        n_scanned += len(chunk)
        times = pd.to_datetime(chunk[TIME], utc=True, errors="coerce")
        if times.isna().any():
            raise ValueError("Unparseable prediction_time_utc values")
        for name, begin, end in boundaries:
            part = chunk.loc[(times >= begin) & (times < end)].copy()
            if name == "train" and keep < 1 and len(part):
                # Must match the original trainer's RNG draws for a fair comparison.
                part = part.loc[rng.random(len(part)) < keep]
            if len(part):
                frames[name].append(part)
    print(f"Scanned {n_scanned:,} source rows.")
    result = {}
    for name, pieces in frames.items():
        if not pieces:
            raise ValueError(f"No rows in the {name} split")
        part = pd.concat(pieces, ignore_index=True)
        part[TARGET] = pd.to_numeric(part[TARGET], errors="raise").astype("int8")
        if not set(part[TARGET].unique()).issubset({0, 1}):
            raise ValueError(f"Invalid target classes in {name}")
        for feature in FEATURES:
            part[feature] = pd.to_numeric(part[feature], errors="coerce").astype("float32")
        result[name] = part
        print(f"{name:>10}: {len(part):,} site-hours; {int(part[TARGET].sum()):,} positive")
    return result


def score_partition(y, proba, train_prevalence):
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    n = len(y)
    pos = int(y.sum())
    entry = {
        "rows": n,
        "positive_site_hours": pos,
        "site_hour_prevalence": float(y.mean()),
        "brier": float(brier_score_loss(y, proba)),
        "baseline_brier_using_train_prevalence": float(
            brier_score_loss(y, np.full(n, train_prevalence))
        ),
        "mean_predicted_probability": float(proba.mean()),
    }
    if 0 < pos < n:
        entry.update({
            "average_precision": float(average_precision_score(y, proba)),
            "no_skill_average_precision": float(y.mean()),
            "roc_auc": float(roc_auc_score(y, proba)),
        })
    else:
        entry["warning"] = "One class only; AP/ROC AUC undefined"
    return entry


def confusion(y, proba, threshold):
    pred = proba >= threshold
    tp = int(np.sum(pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    fn = int(np.sum(~pred & (y == 1)))
    return {
        "tp_site_hours": tp,
        "fp_site_hours": fp,
        "fn_site_hours": fn,
        "precision": tp / max(1, tp + fp),
        "recall": tp / max(1, tp + fn),
        "alerts_per_1000_site_hours": 1000 * (tp + fp) / len(y),
    }


def choose_threshold_from_validation(y, proba):
    from sklearn.metrics import precision_recall_curve
    if y.sum() == 0 or y.sum() == len(y):
        raise ValueError("Validation has only one class; cannot select an F1 threshold")
    p, r, thresholds = precision_recall_curve(y, proba)
    if len(thresholds) == 0:
        raise ValueError("No available validation thresholds")
    f1 = 2 * p[:-1] * r[:-1] / np.maximum(p[:-1] + r[:-1], 1e-12)
    return float(thresholds[int(np.argmax(f1))])


def comparison(calendar: dict, existing_file: Path) -> tuple[dict | None, str]:
    if not existing_file.exists():
        return None, ("The existing full-weather baseline metrics were not found at "
                      f"{existing_file}\nCalendar metrics are saved; run the original trainer "
                      "to create a weather-model comparison.\n")
    weather = json.loads(existing_file.read_text(encoding="utf-8"))
    checks = []
    if weather.get("train_random_subsample_fraction") != calendar["train_random_subsample_fraction"]:
        checks.append("Training subsample fraction differs")
    for split in ["validation", "test"]:
        a = calendar["splits"][split]
        b = weather.get("splits", {}).get(split, {})
        for key in ["rows", "positive_site_hours"]:
            if a.get(key) != b.get(key):
                checks.append(f"{split}: {key} differs: calendar={a.get(key)}, weather={b.get(key)}")
    result = {
        "calendar_features": FEATURES,
        "weather_features": weather.get("features"),
        "calendar_model_threshold": calendar["validation_f1_selected_threshold"],
        "weather_model_threshold": weather.get("validation_f1_selected_threshold"),
        "comparability_warnings": checks,
        "splits": {},
    }
    lines = ["PlanIT | 2025 calendar-only versus full-weather baseline",
             "RESEARCH ONLY; scores apply to site-hours, not distinct storms.",
             "Calendar uses hour/month only; full model also uses station/weather inputs.",
             "Both use the same chronological partitions and the same 35%-style train-only sampling (when settings match).",
             "Each model selects its OWN threshold using validation only.",
             ""]
    for split in ["validation", "test"]:
        a = calendar["splits"][split]
        b = weather["splits"][split]
        summary = {}
        lines.append(f"{split.upper()}:")
        for label, key in [("Average precision", "average_precision"),
                           ("ROC-AUC", "roc_auc"), ("Brier score (lower better)", "brier")]:
            av, bv = a.get(key), b.get(key)
            if av is not None and bv is not None:
                summary[key] = {"calendar": av, "weather": bv, "weather_minus_calendar": bv-av}
                lines.append(f"  {label}: calendar={av:.6f}, weather={bv:.6f}, weather-calendar={bv-av:+.6f}")
        for label, key in [("Precision at validation-chosen threshold", "precision"),
                           ("Recall at validation-chosen threshold", "recall"),
                           ("Positive flags per 1,000 site-hours", "alerts_per_1000_site_hours")]:
            ac = a.get("at_validation_threshold", {})
            bc = b.get("at_validation_threshold", {})
            av, bv = ac.get(key), bc.get(key)
            if av is not None and bv is not None:
                summary[key] = {"calendar": av, "weather": bv, "weather_minus_calendar": bv-av}
                lines.append(f"  {label}: calendar={av:.6f}, weather={bv:.6f}")
        result["splits"][split] = summary
        lines.append("")
    if checks:
        lines.append("WARNING: NOT LIKE-FOR-LIKE: " + "; ".join(checks))
    lines.extend([
        "Interpret AP first, especially on test; high ROC-AUC does not guarantee useful rare-event alerts.",
        "Ablation estimates incremental signal, not proof of model causality or out-of-site generalization.",
        "Validation threshold comparisons may operate at different alert workloads.",
        "Use event-level diagnostic for both models before making alert-workload claims.",
    ])
    return result, "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, default=DATA)
    ap.add_argument("--output", type=Path, default=OUT)
    ap.add_argument("--weather-metrics", type=Path, default=WEATHER_METRICS)
    ap.add_argument("--train-keep", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-iter", type=int, default=120)
    ap.add_argument("--chunksize", type=int, default=100_000)
    args = ap.parse_args()
    if not (0 < args.train_keep <= 1):
        ap.error("--train-keep must be in (0,1]")
    if args.chunksize < 1:
        ap.error("--chunksize must be >=1")
    if not args.input.exists():
        ap.error(f"Dataset not found: {args.input}")
    from sklearn.ensemble import HistGradientBoostingClassifier
    import joblib

    data = read_splits(args.input, args.train_keep, args.seed, args.chunksize)
    ytrain = data["train"][TARGET].to_numpy()
    if len(np.unique(ytrain)) < 2:
        raise ValueError("Training set contains only one class")
    model = HistGradientBoostingClassifier(
        max_iter=args.max_iter,
        learning_rate=0.05,
        max_leaf_nodes=15,
        min_samples_leaf=80,
        l2_regularization=10,
        early_stopping=False,
        random_state=args.seed,
    )
    print("Fitting calendar-only model (4 features; no weather or station distance)...")
    model.fit(data["train"][FEATURES], ytrain)
    train_prev = float(ytrain.mean())
    metrics = {
        "definition": "NEW severe thunderstorm polygon warning within next 6h",
        "model_description": "Calendar-only supervised baseline (hour/month sine/cosine)",
        "train_random_subsample_fraction": args.train_keep,
        "seed": args.seed,
        "max_iter": args.max_iter,
        "features": FEATURES,
        "train_prevalence_sample": train_prev,
        "splits": {},
    }
    for split in ("validation", "test"):
        y = data[split][TARGET].to_numpy()
        probs = model.predict_proba(data[split][FEATURES])[:, 1]
        entry = score_partition(y, probs, train_prev)
        if split == "validation":
            metrics["validation_f1_selected_threshold"] = choose_threshold_from_validation(y, probs)
        entry["at_validation_threshold"] = confusion(y, probs, metrics["validation_f1_selected_threshold"])
        metrics["splits"][split] = entry
        print(split.upper(), json.dumps(entry, indent=2))
    args.output.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES,
                 "caveat": "2025 research-only calendar baseline"},
                args.output / "calendar_hgb.joblib")
    (args.output / "calendar_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    report, report_text = comparison(metrics, args.weather_metrics)
    (args.output / "comparison_with_weather.txt").write_text(report_text, encoding="utf-8")
    if report is not None:
        (args.output / "comparison_with_weather.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
    print("\n" + report_text)
    print("Saved to:", args.output.resolve())
    print("No weather files downloaded. No changes to full-weather baseline.")


if __name__ == "__main__":
    main()
