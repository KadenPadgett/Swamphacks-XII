"""Score rows with an EXISTING PlanIT 2025 frozen model. Research only.

Requires an already precomputed 25-feature table; this does NOT fetch weather,
select a station, calculate 3-hour trends, or decide work safety.

Examples:
  py -3.13 predict_frozen_model.py --model geographic --rows 10
  py -3.13 predict_frozen_model.py --model baseline --input my_feature_rows.csv --rows 100
"""
from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data/processed/development_300_2025/dev300_hourly_training_2025.csv.gz'
ARTIFACTS = {
    'baseline': ROOT / 'data/processed/development_300_2025/model_baseline/baseline_hgb.joblib',
    'geographic': ROOT / 'data/processed/development_300_2025/model_geographic_holdout/geographic_hgb.joblib',
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', choices=sorted(ARTIFACTS), default='geographic')
    p.add_argument('--input', type=Path, default=DATA,
                   help='CSV or CSV.gz of PRECOMPUTED model features')
    p.add_argument('--rows', type=int, default=10, help='Maximum rows to score')
    p.add_argument('--output', type=Path, default=ROOT / 'prediction_preview.csv')
    args = p.parse_args()
    if args.rows < 1:
        p.error('--rows must be >= 1')
    model_path = ARTIFACTS[args.model]
    if not model_path.is_file():
        p.error(f'Missing saved model: {model_path}')
    if not args.input.is_file():
        p.error(f'Missing feature table: {args.input}')
    # Only load saved models from a trusted source: joblib uses pickle internals.
    artifact = joblib.load(model_path)
    if not isinstance(artifact, dict) or 'model' not in artifact or 'features' not in artifact:
        raise ValueError('Invalid saved model bundle; expected model and feature list')
    features = list(artifact['features'])
    sample = pd.read_csv(args.input, nrows=args.rows, low_memory=False)
    absent = sorted(set(features) - set(sample.columns))
    if absent:
        raise ValueError(f'Missing required precomputed features: {absent}')
    X = sample.loc[:, features].apply(pd.to_numeric, errors='coerce').astype('float32')
    probabilities = artifact['model'].predict_proba(X)[:, 1]
    if not np.isfinite(probabilities).all():
        raise ValueError('Non-finite prediction')
    identifiers = [c for c in ('plant_code', 'prediction_time_utc') if c in sample.columns]
    output = sample.loc[:, identifiers].copy()
    output['research_warning_probability_next_6h'] = probabilities
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    print(f'Model: {args.model} ({model_path.name})')
    print(f'Feature count: {len(features)}; scored rows: {len(output)}')
    print(f'Saved: {args.output}')
    print(output.head(10).to_string(index=False))
    print('CAUTION: scores have NOT been validated for safety decisions or calibrated deployment.')


if __name__ == '__main__':
    main()
