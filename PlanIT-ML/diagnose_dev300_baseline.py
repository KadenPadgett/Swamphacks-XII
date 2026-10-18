"""PlanIT research diagnostic: inspect temporal holdout baseline WITHOUT retuning on test.

Run from risk_assessment:
    py -3.13 diagnose_dev300_baseline.py

Inputs (already generated):
    data/processed/development_300_2025/dev300_hourly_training_2025.csv.gz
    data/processed/development_300_2025/model_baseline/baseline_hgb.joblib
    data/processed/development_300_2025/model_baseline/baseline_metrics.json
    data/processed/plant_warning_events_2025.csv

Outputs: data/processed/development_300_2025/model_baseline/diagnostics/
    summary.txt
    temporal_subgroups.csv
    probability_bins.csv
    warning_event_detection.csv
    predicted_alert_episodes.csv

This is NOT an alerting or operational safety system. It preserves the existing
validation-selected threshold and does not train/tune another model.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

ROOT = Path(__file__).resolve().parent
DEV = ROOT / 'data/processed/development_300_2025'
DEFAULT_DATA = DEV / 'dev300_hourly_training_2025.csv.gz'
DEFAULT_MODEL = DEV / 'model_baseline/baseline_hgb.joblib'
DEFAULT_METRICS = DEV / 'model_baseline/baseline_metrics.json'
DEFAULT_EVENTS = ROOT / 'data/processed/plant_warning_events_2025.csv'
DEFAULT_OUT = DEV / 'model_baseline/diagnostics'
SPLITS = {
    'validation': ('2025-09-01T00:00:00Z', '2025-10-31T18:00:00Z'),
    'test': ('2025-11-01T00:00:00Z', '2025-12-31T18:00:00Z'),
}
TARGET = 'label_new_sv_warning_next_6h'


def evaluate_partition(part: pd.DataFrame, key: str, value: str) -> dict:
    y = part[TARGET].to_numpy(dtype=int)
    p = part['model_probability'].to_numpy(dtype=float)
    flags = part['predicted_flag'].to_numpy(dtype=bool)
    tp = int(np.sum(flags & (y == 1)))
    fp = int(np.sum(flags & (y == 0)))
    fn = int(np.sum(~flags & (y == 1)))
    n = int(len(part))
    result = {'dimension': key, 'group': value, 'site_hours': n,
              'positive_site_hours': int(y.sum()),
              'prevalence': float(y.mean()), 'predicted_positive_site_hours': int(flags.sum()),
              'tp': tp, 'fp': fp, 'fn': fn,
              'precision': tp / max(1, tp + fp), 'recall': tp / max(1, tp + fn),
              'mean_prediction': float(p.mean())}
    result['average_precision'] = float(average_precision_score(y, p)) if 0 < y.sum() < n else np.nan
    result['roc_auc'] = float(roc_auc_score(y, p)) if 0 < y.sum() < n else np.nan
    return result


def read_and_score(data_path: Path, artifact: dict, threshold: float, chunk_size: int) -> pd.DataFrame:
    needed = ['plant_code', 'plant_state', 'distance_km', 'has_station_report',
              'prediction_time_utc', TARGET, *artifact['features']]
    needed = list(dict.fromkeys(needed))
    pieces = []
    scan = 0
    for chunk in pd.read_csv(data_path, usecols=needed, chunksize=chunk_size,
                             dtype={'plant_code': 'string'}, low_memory=False):
        scan += len(chunk)
        stamp = pd.to_datetime(chunk['prediction_time_utc'], errors='coerce', utc=True)
        masks = []
        for name, (begin, end) in SPLITS.items():
            masks.append((stamp >= pd.Timestamp(begin)) & (stamp < pd.Timestamp(end)))
        mask = masks[0] | masks[1]
        if not mask.any():
            continue
        part = chunk.loc[mask].copy()
        part['time'] = stamp.loc[mask]
        part['split'] = np.where(masks[0].loc[mask], 'validation', 'test')
        x = part[artifact['features']].apply(pd.to_numeric, errors='coerce').astype('float32')
        part['model_probability'] = artifact['model'].predict_proba(x)[:, 1]
        part['predicted_flag'] = part.model_probability >= threshold
        part[TARGET] = pd.to_numeric(part[TARGET], errors='raise').astype('int8')
        keep = ['plant_code', 'plant_state', 'distance_km', 'has_station_report', 'time',
                'split', TARGET, 'model_probability', 'predicted_flag']
        pieces.append(part[keep])
    if not pieces:
        raise ValueError('No validation/test rows identified; inspect source timestamps')
    print(f'Scanned {scan:,} source rows; retained {sum(map(len, pieces)):,} validation/test rows.')
    return pd.concat(pieces, ignore_index=True)


def summarize_groups(scored: pd.DataFrame) -> pd.DataFrame:
    df = scored.copy()
    df['month'] = df.time.dt.strftime('%Y-%m')
    df['distance_band'] = pd.cut(
        pd.to_numeric(df.distance_km, errors='coerce'),
        bins=[-np.inf, 10, 25, 50, np.inf], labels=['<10km', '10-25km', '25-50km', '50km+'])
    df['weather_coverage'] = np.where(df.has_station_report == 1, 'METAR available', 'METAR missing')
    results = []
    for split, frame in df.groupby('split', sort=False):
        results.append(evaluate_partition(frame, 'all', split))
        for column in ['month', 'distance_band', 'weather_coverage']:
            for group, part in frame.groupby(column, observed=True, dropna=False):
                results.append(evaluate_partition(part, f'{split}:{column}', str(group)))
    return pd.DataFrame(results)


def probability_bins(scored: pd.DataFrame) -> pd.DataFrame:
    # Stable, interpretable fixed absolute probability bins (not fitted on test).
    cuts = [-1e-10, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0]
    labels = ['<0.01%', '0.01-0.05%', '0.05-0.1%', '0.1-0.5%', '0.5-1%',
              '1-2.5%', '2.5-5%', '5-10%', '10-25%', '25-100%']
    df = scored.copy()
    df['probability_bin'] = pd.cut(df.model_probability, cuts, labels=labels,
                                   right=False, include_lowest=True)
    grp = df.groupby(['split', 'probability_bin'], observed=True)
    report = grp.agg(site_hours=(TARGET, 'size'), positives=(TARGET, 'sum'),
                     predicted_mean=('model_probability', 'mean')).reset_index()
    report['observed_frequency'] = report['positives'] / report['site_hours']
    return report


def get_events(path: Path) -> pd.DataFrame:
    """Use the exact timestamp parser that created labels, not a new interpretation."""
    from build_dev300_v2 import parse_warning_timestamps
    probe = pd.read_csv(path, nrows=0)
    plant_column = next((c for c in ('plant_code', 'plant code') if c in probe.columns), None)
    if plant_column is None or 'INIT_ISS' not in probe.columns:
        raise ValueError(f'Expected plant_code or plant code, and INIT_ISS; got {list(probe.columns)}')
    optional = [x for x in ('WFO', 'ETN') if x in probe.columns]
    ev = pd.read_csv(path, usecols=[plant_column, 'INIT_ISS', *optional],
                     dtype={plant_column: 'string'}, low_memory=False)
    ev = ev.rename(columns={plant_column: 'plant_code'})
    print(f'Event plant identifier column: {plant_column!r} -> plant_code')
    ev['issuance'], parser = parse_warning_timestamps(ev.INIT_ISS)
    print('Event timestamp parser:', parser)
    ev = ev.dropna(subset=['plant_code', 'issuance'])
    ev = ev.loc[(ev.issuance >= pd.Timestamp('2025-01-01T00:00Z')) &
                (ev.issuance < pd.Timestamp('2026-01-01T00:00Z'))].copy()
    ev['plant_code'] = ev.plant_code.astype('string').str.strip()
    # One site + original initial issuance counts as one site event for detection.
    ev = ev.drop_duplicates(subset=['plant_code', 'issuance'])
    return ev


def event_detection(scored: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    frames = {code: frame.sort_values('time') for code, frame in scored.groupby('plant_code')}
    results = []
    six_hours = pd.Timedelta(hours=6)
    for e in events.itertuples(index=False):
        code = str(e.plant_code)
        plant = frames.get(code)
        if plant is None:
            continue
        eligible = plant.loc[(plant.time < e.issuance) & (plant.time + six_hours >= e.issuance)]
        if eligible.empty:
            continue
        # A warning can be reachable from only one evaluation split except boundaries.
        for split, subset in eligible.groupby('split'):
            results.append({
                'plant_code': code, 'split': split, 'issuance_utc': e.issuance,
                'hours_in_lookback': len(subset),
                'caught_at_fixed_threshold': bool(subset.predicted_flag.any()),
                'max_probability_in_lookback': float(subset.model_probability.max()),
                'first_positive_prediction_utc': subset.loc[subset.predicted_flag, 'time'].min()
                if subset.predicted_flag.any() else pd.NaT,
            })
    return pd.DataFrame(results, columns=['plant_code', 'split', 'issuance_utc',
                'hours_in_lookback', 'caught_at_fixed_threshold',
                'max_probability_in_lookback', 'first_positive_prediction_utc'])


def alert_episodes(scored: pd.DataFrame) -> pd.DataFrame:
    # A new episode begins when >6h separate positive hourly model flags at a site.
    # Episode counts are a more interpretable workload measure than raw flag counts.
    results = []
    for (split, code), frame in scored.loc[scored.predicted_flag].groupby(['split', 'plant_code']):
        frame = frame.sort_values('time')
        groups = frame.time.diff().gt(pd.Timedelta(hours=6)).fillna(True).cumsum()
        for _, ep in frame.groupby(groups):
            results.append({'split': split, 'plant_code': code,
                            'start_utc': ep.time.iloc[0], 'end_utc': ep.time.iloc[-1],
                            'flagged_site_hours': len(ep),
                            'any_positive_labeled_hour': bool(ep[TARGET].any()),
                            'peak_predicted_probability': float(ep.model_probability.max())})
    return pd.DataFrame(results, columns=['split', 'plant_code', 'start_utc', 'end_utc',
                                           'flagged_site_hours', 'any_positive_labeled_hour',
                                           'peak_predicted_probability'])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, default=DEFAULT_DATA)
    p.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    p.add_argument('--metrics', type=Path, default=DEFAULT_METRICS)
    p.add_argument('--events', type=Path, default=DEFAULT_EVENTS)
    p.add_argument('--out', type=Path, default=DEFAULT_OUT)
    p.add_argument('--chunk-size', type=int, default=100_000)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    artifact = joblib.load(args.model)  # only load your own locally generated .joblib files
    info = json.loads(args.metrics.read_text(encoding='utf-8'))
    threshold = float(info['validation_f1_selected_threshold'])
    print(f'Fixed validation-selected threshold: {threshold:.8f} (not retuned)')
    scored = read_and_score(args.dataset, artifact, threshold, args.chunk_size)
    subgroups = summarize_groups(scored)
    subgroups.to_csv(args.out/'temporal_subgroups.csv', index=False)
    probability_bins(scored).to_csv(args.out/'probability_bins.csv', index=False)
    episodes = alert_episodes(scored)
    episodes.to_csv(args.out/'predicted_alert_episodes.csv', index=False)
    events = get_events(args.events)
    detected = event_detection(scored, events)
    detected.to_csv(args.out/'warning_event_detection.csv', index=False)

    lines = [
        'PlanIT | baseline diagnostics (NOT operational)',
        f'Fixed threshold from validation: {threshold:.8f}',
        'No new model fit, no test-set threshold tuning.',
        'Distinct issuance at one plant is a site-warning event, not a distinct storm.',
        'Event detected = >=1 positive model flag in the preceding 6-hour prediction window.',
        'Alert episode = model-positive site-hours no more than 6 hours apart.',
        '',
    ]
    for split in ('validation', 'test'):
        ds = scored.loc[scored.split == split]
        es = detected.loc[detected.split == split]
        eps = episodes.loc[episodes.split == split]
        y = ds[TARGET].to_numpy(dtype=int)
        p_scores = ds.model_probability.to_numpy()
        n_pos = int(y.sum())
        n_reached = int(es.caught_at_fixed_threshold.sum()) if len(es) else 0
        lines.extend([
            f'{split.upper()}:',
            f'  Site-hours: {len(ds):,}; positives: {n_pos:,}',
            f'  Average precision: {average_precision_score(y, p_scores):.6f}' if 0 < n_pos < len(ds) else '  Average precision: not defined',
            f'  Plants with >=1 positive label: {ds.loc[ds[TARGET] == 1, "plant_code"].nunique()}',
            f'  Distinct site-warning issuance events reachable in split: {len(es):,}',
            f'  Site-warning issuance events detected: {n_reached:,}/{len(es):,} ({100*n_reached/max(1,len(es)):.1f}%)',
            f'  Predicted alert episodes: {len(eps):,}',
            f'  Episodes containing >=1 labeled-positive hour: {int(eps.any_positive_labeled_hour.sum()):,}' if len(eps) else '  Episodes containing >=1 labeled-positive hour: 0',
            '',
        ])
    lines.extend([
        'LIMITATIONS:',
        '- This quantifies plant-warning issuance opportunities, not unique meteorological storms.',
        '- Episodes are heuristic and may merge multiple meteorological events.',
        '- Missing archived warnings can bias labels and calculated false-positive rates.',
        '- Repeated site-hours are correlated; this is not independent-event statistical validation.',
        '- Temporal splits still share power plants and stations; geographic holdout needed.',
        '- Archive METAR availability is based on an assumed 60-minute delay, not actual publication timestamps.',
        '- No results here authorize or determine crew dispatch/stop-work decisions.',
    ])
    result = '\n'.join(lines) + '\n'
    (args.out/'summary.txt').write_text(result, encoding='utf-8')
    print('\n'+result)
    print('Saved diagnostics to:', args.out)


if __name__ == '__main__':
    main()
