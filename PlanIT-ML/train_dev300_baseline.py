"""PlanIT 300-plant RESEARCH baseline. Never use to authorize field work.

Requires: py -3.13 -m pip install scikit-learn pandas numpy joblib
Run in risk_assessment: py -3.13 train_dev300_baseline.py

Chronological periods with a >=6h embargo at each boundary:
  Train: Jan 1 through Aug 31 17:00 UTC
  Val:   Sep 1 through Oct 31 17:00 UTC
  Test:  Nov 1 through Dec 31 17:00 UTC
The label looks ahead six hours and must not cross into the next split.

Train-only uniform downsampling of rows (default 35%) is independent of label;
validation and test stay at natural prevalence. Never fit encoders/imputers on validation/test.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / 'data/processed/development_300_2025/dev300_hourly_training_2025.csv.gz'
OUT = ROOT / 'data/processed/development_300_2025/model_baseline'
TARGET = 'label_new_sv_warning_next_6h'
TIME = 'prediction_time_utc'
FEATURES = [
    'distance_km', 'precip_trace_reported', 'tmpf', 'dwpf', 'relh',
    'drct', 'sknt', 'gust', 'p01i', 'alti', 'mslp', 'vsby',
    'has_station_report', 'observation_age_minutes', 'gust_reported',
    'mslp_reported', 'alti_reported', 'mslp_change_3h',
    'tmpf_change_3h', 'relh_change_3h', 'sknt_change_3h',
    'hour_sin', 'hour_cos', 'month_sin', 'month_cos',
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', type=Path, default=INPUT)
    ap.add_argument('--train-keep', type=float, default=0.35,
                    help='Fraction of train rows retained independently of label; 1 keeps all.')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--max-iter', type=int, default=120)
    args = ap.parse_args()
    if not (0 < args.train_keep <= 1):
        ap.error('--train-keep must be in (0,1]')

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss, precision_recall_curve
    import joblib

    rng = np.random.default_rng(args.seed)
    frames = {'train': [], 'validation': [], 'test': []}
    boundaries = [
        ('train', pd.Timestamp('2025-01-01T00:00Z'), pd.Timestamp('2025-08-31T18:00Z')),
        ('validation', pd.Timestamp('2025-09-01T00:00Z'), pd.Timestamp('2025-10-31T18:00Z')),
        ('test', pd.Timestamp('2025-11-01T00:00Z'), pd.Timestamp('2025-12-31T18:00Z')),
    ]
    if not args.input.exists():
        raise FileNotFoundError(args.input)
    cols = FEATURES + [TARGET, TIME]
    for chunk in pd.read_csv(args.input, usecols=cols, chunksize=100_000, low_memory=False):
        times = pd.to_datetime(chunk[TIME], utc=True, errors='coerce')
        if times.isna().any():
            raise ValueError('Unparseable prediction timestamp')
        for name, start, stop in boundaries:
            part = chunk.loc[(times >= start) & (times < stop)].copy()
            if name == 'train' and args.train_keep < 1 and len(part):
                part = part.loc[rng.random(len(part)) < args.train_keep]
            if len(part):
                frames[name].append(part)
    data = {}
    for name, pieces in frames.items():
        if not pieces:
            raise ValueError(f'{name}: no rows after date filtering')
        data[name] = pd.concat(pieces, ignore_index=True)
        data[name][TARGET] = pd.to_numeric(data[name][TARGET], errors='raise').astype('int8')
        for col in FEATURES:
            data[name][col] = pd.to_numeric(data[name][col], errors='coerce').astype('float32')
        print(f'{name:>10}: {len(data[name]):,} rows; positive {int(data[name][TARGET].sum()):,}')

    xtrain = data['train'][FEATURES]
    ytrain = data['train'][TARGET].to_numpy()
    if len(np.unique(ytrain)) < 2:
        raise ValueError('Training has only one class; expand dates / train sample')
    model = HistGradientBoostingClassifier(
        max_iter=args.max_iter, learning_rate=0.05, max_leaf_nodes=15,
        min_samples_leaf=80, l2_regularization=10,
        early_stopping=False, random_state=args.seed,
    )
    print('Fitting classifier...')
    model.fit(xtrain, ytrain)
    baseline = float(ytrain.mean())
    metrics = {'definition': 'NEW severe thunderstorm polygon warning within next 6h',
               'train_random_subsample_fraction': args.train_keep,
               'features': FEATURES, 'train_prevalence_sample': baseline,
               'splits': {}}
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ('validation', 'test'):
        target = data[name][TARGET].to_numpy()
        probabilities = model.predict_proba(data[name][FEATURES])[:, 1]
        positives = int(target.sum())
        count = len(target)
        entry = {'rows': count, 'positive_site_hours': positives,
                 'site_hour_prevalence': float(target.mean()),
                 'brier': float(brier_score_loss(target, probabilities)),
                 'baseline_brier_using_train_prevalence': float(brier_score_loss(target, np.full(count, baseline))),
                 'mean_predicted_probability': float(probabilities.mean())}
        if 0 < positives < count:
            entry['average_precision'] = float(average_precision_score(target, probabilities))
            entry['no_skill_average_precision'] = float(target.mean())
            entry['roc_auc'] = float(roc_auc_score(target, probabilities))
            # Diagnostic threshold selected on validation only, applied unchanged on test.
            precision, recall, thresholds = precision_recall_curve(target, probabilities)
            if name == 'validation':
                f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
                best = int(np.argmax(f1))
                selected_threshold = float(thresholds[best]) if len(thresholds) else 0.5
                metrics['validation_f1_selected_threshold'] = selected_threshold
            if 'validation_f1_selected_threshold' in metrics:
                p = probabilities >= metrics['validation_f1_selected_threshold']
                tp = int(((p == 1) & (target == 1)).sum())
                fp = int(((p == 1) & (target == 0)).sum())
                fn = int(((p == 0) & (target == 1)).sum())
                entry['at_validation_threshold'] = {
                    'tp_site_hours': tp, 'fp_site_hours': fp, 'fn_site_hours': fn,
                    'precision': tp / max(tp+fp, 1), 'recall': tp / max(tp+fn, 1),
                    'alerts_per_1000_site_hours': 1000 * (tp + fp) / count,
                }
        else:
            entry['warning'] = 'Only one class: average precision/ROC AUC not meaningful'
        metrics['splits'][name] = entry
        print(name, json.dumps(entry, indent=2))
    joblib.dump({'model': model, 'features': FEATURES, 'caveat': 'research-only 2025 baseline'}, OUT/'baseline_hgb.joblib')
    (OUT/'baseline_metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    print('Saved:', OUT/'baseline_metrics.json')
    print('IMPORTANT: one-year temporal test is seasonal and site-overlapping; add geographic holdout and multi-year data.')

if __name__ == '__main__':
    main()
