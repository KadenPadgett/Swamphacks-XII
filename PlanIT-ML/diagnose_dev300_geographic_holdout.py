"""PlanIT geographic score diagnostics: existing 2025 model, NO retraining.

Place in risk_assessment and run:
    py -3.13 diagnose_dev300_geographic_holdout.py

Reads dev300_hourly_training_2025.csv.gz and the geographic experiment's
geographic_hgb.joblib, geographic_metrics.json, geographic_site_assignment.csv.

Uses the ORIGINAL validation-F1 threshold and additional thresholds determined
ONLY from the DEVELOPMENT-PLANT Sep-Oct validation scores at fixed flag-budget
levels. Test outcomes never select a threshold. Provides descriptive diagnostic
curves, not alternative deployment thresholds.

Optional archived issuance events, if the CSV is available, are counted as
plant-warning issuance opportunities, NOT independent storms.

No model fitting, feature change, weather downloads, or safety decisions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss

ROOT = Path(__file__).resolve().parent
DEV = ROOT / 'data/processed/development_300_2025'
DEFAULT_MODEL_DIR = DEV / 'model_geographic_holdout'
DEFAULT_EVENTS = ROOT / 'data/processed/plant_warning_events_2025.csv'
TARGET = 'label_new_sv_warning_next_6h'
PERIODS = {
    'validation': ('2025-09-01T00:00:00Z', '2025-10-31T18:00:00Z'),
    'known_site_test': ('2025-11-01T00:00:00Z', '2025-12-31T18:00:00Z'),
    'unseen_site_test': ('2025-11-01T00:00:00Z', '2025-12-31T18:00:00Z'),
}
# Interpret as hourly site-flags per 1,000 observed site-hours, NOT episodes.
BUDGETS = (0.5, 1.0, 2.0, 5.0, 10.0)
PERCENTILES = (0, 10, 25, 50, 75, 90, 95, 99, 100)


def check_artifacts(folder: Path):
    config = json.loads((folder / 'geographic_metrics.json').read_text(encoding='utf-8'))
    artifact = joblib.load(folder / 'geographic_hgb.joblib')  # TRUSTED LOCAL ARTIFACT ONLY
    mapping = pd.read_csv(folder / 'geographic_site_assignment.csv', dtype={'plant_code': 'string'})
    required = {'plant_code', 'geographic_partition'}
    if not required.issubset(mapping.columns):
        raise ValueError(f'Geographic site assignment missing {required - set(mapping.columns)}')
    if mapping.plant_code.duplicated().any() or mapping.plant_code.isna().any():
        raise ValueError('Duplicate or null plant codes in assignment')
    partition = dict(zip(mapping.plant_code.astype(str), mapping.geographic_partition))
    if set(mapping.geographic_partition) != {'development', 'unseen_geographic_test'}:
        raise ValueError('Unexpected geographic_partition values')
    features = list(artifact['features'])
    if features != list(config['features']):
        raise ValueError('Model and saved metrics specify different feature order')
    threshold = float(config['threshold_selected_from_development_validation_only'])
    if not np.isfinite(threshold) or not (0 <= threshold <= 1):
        raise ValueError('Invalid validation-selected threshold in saved metrics')
    return config, artifact, partition, features, threshold


def score_csv(dataset: Path, artifact, partition: dict, features: list[str], chunk_size: int):
    needed = list(dict.fromkeys(['plant_code', 'prediction_time_utc', TARGET, *features]))
    out = []
    seen = 0
    for chunk in pd.read_csv(dataset, usecols=needed, chunksize=chunk_size,
                             low_memory=False, dtype={'plant_code': 'string'}):
        seen += len(chunk)
        ts = pd.to_datetime(chunk.prediction_time_utc, utc=True, errors='coerce')
        if ts.isna().any():
            raise ValueError('Unparseable prediction timestamp')
        dates_val = ts.ge(PERIODS['validation'][0]) & ts.lt(PERIODS['validation'][1])
        dates_test = ts.ge(PERIODS['known_site_test'][0]) & ts.lt(PERIODS['known_site_test'][1])
        use = dates_val | dates_test
        if not use.any():
            continue
        part = chunk.loc[use]
        plant = part.plant_code.astype(str)
        site = plant.map(partition)
        if site.isna().any():
            raise ValueError(f'Unassigned plant codes in dataset: {plant[site.isna()].unique()[:5]}')
        val = dates_val.loc[use] & site.eq('development')
        known = dates_test.loc[use] & site.eq('development')
        unseen = dates_test.loc[use] & site.eq('unseen_geographic_test')
        keep = val | known | unseen
        if not keep.any():
            continue
        part = part.loc[keep]
        x = part[features].apply(pd.to_numeric, errors='coerce').astype('float32')
        scores = artifact['model'].predict_proba(x)[:, 1]
        if not np.isfinite(scores).all():
            raise ValueError('Nonfinite model probability')
        idx = part.index
        group = np.select([val.loc[idx], known.loc[idx], unseen.loc[idx]],
                          ['validation', 'known_site_test', 'unseen_site_test'], default='ERROR')
        out.append(pd.DataFrame({
            'plant_code': part.plant_code.astype(str).to_numpy(),
            'time': ts.loc[idx].to_numpy(),
            'split': group,
            TARGET: pd.to_numeric(part[TARGET], errors='raise').astype('int8').to_numpy(),
            'score': scores,
        }))
    if not out:
        raise ValueError('No rows matched geographic validation/test periods')
    scored = pd.concat(out, ignore_index=True)
    if 'ERROR' in set(scored.split):
        raise AssertionError('Unexpected split assignment')
    if scored.duplicated(['plant_code', 'time']).any():
        raise ValueError('Duplicate site/timestamp records in input')
    print(f'Scanned {seen:,} source rows; scored {len(scored):,} eligible rows.')
    return scored


def score_summary(frame):
    y = frame[TARGET].to_numpy(dtype=np.int8)
    p = frame.score.to_numpy(dtype=float)
    result = {
        'site_hours': len(frame), 'plants': frame.plant_code.nunique(),
        'positive_site_hours': int(y.sum()), 'prevalence': float(y.mean()),
        'predicted_mean': float(p.mean()), 'brier': float(brier_score_loss(y, p)),
    }
    if 0 < y.sum() < len(y):
        result['average_precision'] = float(average_precision_score(y, p))
        result['roc_auc'] = float(roc_auc_score(y, p))
        result['ap_divided_by_prevalence'] = result['average_precision'] / result['prevalence']
    return result


def score_percentiles(scored):
    rows = []
    for split, frame in scored.groupby('split'):
        for label, sub in [('positive', frame.loc[frame[TARGET] == 1]),
                           ('negative', frame.loc[frame[TARGET] == 0]),
                           ('all', frame)]:
            a = sub.score.to_numpy()
            values = np.percentile(a, PERCENTILES) if len(a) else [np.nan]*len(PERCENTILES)
            for percentile, val in zip(PERCENTILES, values):
                rows.append({'split': split, 'class': label, 'count': len(a),
                             'percentile': percentile, 'score': float(val)})
    return pd.DataFrame(rows)


def validation_budget_thresholds(scored):
    # Derived from validation predictions ONLY; never tune on test labels/scores.
    val = scored.loc[scored.split == 'validation', 'score'].to_numpy(dtype=float)
    thresholds = {}
    descending = np.sort(val)[::-1]
    for budget in BUDGETS:
        # Use the observed unique score as a threshold; ties can exceed the budget.
        k = max(1, int(np.floor(len(val) * budget / 1000)))
        k = min(k, len(descending))
        thresholds[f'validation_budget_{budget:g}_per_1000'] = float(descending[k-1])
    return thresholds


def threshold_report(scored, thresholds):
    rows = []
    for split, frame in scored.groupby('split', sort=False):
        y = frame[TARGET].to_numpy(dtype=bool)
        p = frame.score.to_numpy(dtype=float)
        for name, threshold in thresholds.items():
            predicted = p >= threshold
            tp = int(np.count_nonzero(predicted & y))
            fp = int(np.count_nonzero(predicted & ~y))
            fn = int(np.count_nonzero(~predicted & y))
            flags = tp + fp
            rows.append({'split': split, 'threshold_policy': name,
                         'threshold_from_validation': threshold,
                         'site_hours': len(frame), 'positive_site_hours': int(y.sum()),
                         'flagged_site_hours': flags, 'tp': tp, 'fp': fp, 'fn': fn,
                         'precision': tp / flags if flags else 0.,
                         'recall': tp / (tp + fn) if tp + fn else np.nan,
                         'flags_per_1000_site_hours': 1000 * flags / len(frame),
                         'positive_flag_count': tp, 'negative_flag_count': fp})
    return pd.DataFrame(rows)


def events_file(path):
    from build_dev300_v2 import parse_warning_timestamps
    columns = pd.read_csv(path, nrows=0).columns
    plant_col = next((c for c in ('plant_code', 'plant code') if c in columns), None)
    if plant_col is None or 'INIT_ISS' not in columns:
        raise ValueError(f'Event CSV missing plant code or INIT_ISS; found {list(columns)}')
    e = pd.read_csv(path, usecols=[plant_col, 'INIT_ISS'],
                    dtype={plant_col: 'string'}, low_memory=False)
    e = e.rename(columns={plant_col: 'plant_code'})
    e['issuance'], decoder = parse_warning_timestamps(e.INIT_ISS)
    print(f'Parsed archived warning issuance timestamps via: {decoder}')
    e['plant_code'] = e.plant_code.astype('string').str.strip()
    e = e.dropna(subset=['plant_code', 'issuance'])
    return e[['plant_code', 'issuance']].drop_duplicates()


def score_event_windows(scored, events):
    """For each plant issuance, maximum observed score in preceding 6h.

    A duplicate at a split boundary is preserved if reachable from multiple
    evaluated time partitions; report separately by split, not summed as storms.
    """
    windows = []
    plants = {p: v.sort_values('time') for p, v in scored.groupby('plant_code')}
    horizon = pd.Timedelta(hours=6)
    for evt in events.itertuples(index=False):
        site = plants.get(str(evt.plant_code))
        if site is None:
            continue
        near = site.loc[(site.time < evt.issuance) & (site.time + horizon >= evt.issuance)]
        if near.empty:
            continue
        for split, frame in near.groupby('split'):
            windows.append({'plant_code': str(evt.plant_code), 'split': split,
                            'issuance_utc': evt.issuance,
                            'lookback_site_hours': len(frame),
                            'max_preissuance_score': float(frame.score.max())})
    return pd.DataFrame(windows, columns=[
        'plant_code', 'split', 'issuance_utc', 'lookback_site_hours', 'max_preissuance_score'])


def event_detection_counts(event_windows, thresholds):
    rows = []
    for split, group in event_windows.groupby('split'):
        max_scores = group.max_preissuance_score.to_numpy(dtype=float)
        for policy, threshold in thresholds.items():
            rows.append({'split': split, 'threshold_policy': policy,
                         'threshold_from_validation': threshold,
                         'reachable_site_warning_issuances': len(group),
                         'detected_site_warning_issuances': int((max_scores >= threshold).sum()),
                         'event_detection_fraction': float((max_scores >= threshold).mean())})
    return pd.DataFrame(rows)


def episode_counts_fixed(scored, threshold):
    # A new episode starts if >6h between flags from same plant in same split.
    rows = []
    flagged = scored.loc[scored.score >= threshold]
    for (split, plant), group in flagged.groupby(['split', 'plant_code'], sort=False):
        frame = group.sort_values('time')
        gid = frame.time.diff().gt(pd.Timedelta(hours=6)).fillna(True).cumsum()
        for _, episode in frame.groupby(gid):
            rows.append({'split': split, 'plant_code': plant,
                         'start_utc': episode.time.iloc[0],
                         'end_utc': episode.time.iloc[-1],
                         'flagged_site_hours': len(episode),
                         'has_positive_labeled_hour': bool(episode[TARGET].any()),
                         'peak_score': float(episode.score.max())})
    return pd.DataFrame(rows, columns=['split', 'plant_code', 'start_utc',
                                       'end_utc', 'flagged_site_hours',
                                       'has_positive_labeled_hour', 'peak_score'])


def check_reproduction(stats, saved):
    # Ensure diagnostic really reproduced the prior fitted-model experiment.
    differences = []
    for split, stat in stats.items():
        historical = saved['splits'][split]
        if stat['site_hours'] != historical['rows'] or stat['positive_site_hours'] != historical['positive_site_hours']:
            raise ValueError(f'{split}: data rows/labels differ from saved experiment: '
                             f'{stat["site_hours"]}/{stat["positive_site_hours"]} vs '
                             f'{historical["rows"]}/{historical["positive_site_hours"]}')
        if abs(stat['average_precision'] - historical['average_precision']) > 1e-7:
            differences.append(f'{split} AP {stat["average_precision"]:.8f} vs saved {historical["average_precision"]:.8f}')
    if differences:
        raise ValueError('Predictions disagree with original fitted-model metrics; inspect the source artifacts: '
                         + '; '.join(differences))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset', type=Path, default=DEV/'dev300_hourly_training_2025.csv.gz')
    p.add_argument('--model-dir', type=Path, default=DEFAULT_MODEL_DIR)
    p.add_argument('--events', type=Path, default=DEFAULT_EVENTS)
    p.add_argument('--out', type=Path, default=DEFAULT_MODEL_DIR/'geographic_diagnostics')
    p.add_argument('--chunk-size', type=int, default=100_000)
    p.add_argument('--save-scored', action='store_true', help='Also save all scored site-hours (CSV.gz)')
    a = p.parse_args()
    if a.chunk_size < 1:
        p.error('chunk-size must be positive')
    for path in (a.dataset, a.model_dir/'geographic_hgb.joblib',
                 a.model_dir/'geographic_metrics.json', a.model_dir/'geographic_site_assignment.csv'):
        if not path.is_file():
            p.error(f'Required input missing: {path}')

    saved, artifact, partition, features, original_threshold = check_artifacts(a.model_dir)
    print(f'Original development-validation F1 threshold (unchanged): {original_threshold:.8f}')
    scored = score_csv(a.dataset, artifact, partition, features, a.chunk_size)
    stats = {name: score_summary(frame) for name, frame in scored.groupby('split')}
    for split in PERIODS:
        if split not in stats:
            raise ValueError(f'Missing partition: {split}')
    check_reproduction(stats, saved)
    thresholds = {'original_validation_F1': original_threshold,
                  **validation_budget_thresholds(scored)}
    table = threshold_report(scored, thresholds)
    percentiles = score_percentiles(scored)
    episodes = episode_counts_fixed(scored, original_threshold)
    events = None
    detections = pd.DataFrame()
    windows = pd.DataFrame()
    if a.events.is_file():
        events = events_file(a.events)
        windows = score_event_windows(scored, events)
        detections = event_detection_counts(windows, thresholds)
    else:
        print(f'Event CSV not found, skipping issuance-event metrics: {a.events}')

    a.out.mkdir(parents=True, exist_ok=True)
    percentiles.to_csv(a.out/'score_percentiles.csv', index=False)
    table.to_csv(a.out/'validation_threshold_sensitivity.csv', index=False)
    episodes.to_csv(a.out/'fixed_threshold_alert_episodes.csv', index=False)
    windows.to_csv(a.out/'warning_event_windows.csv', index=False)
    detections.to_csv(a.out/'event_detection_sensitivity.csv', index=False)
    if a.save_scored:
        scored.to_csv(a.out/'scored_site_hours.csv.gz', index=False, compression='gzip')

    results = {'method': 'frozen geographic model; no training or test threshold selection',
               'validation_F1_threshold': original_threshold,
               'validation_derived_thresholds': thresholds,
               'partitions': stats,
               'events_available': events is not None}
    (a.out/'diagnostics.json').write_text(json.dumps(results, indent=2), encoding='utf-8')

    lines = [
        'PlanIT | Geographic diagnostic (RESEARCH ONLY)',
        f'Original Sep-Oct development-site validation threshold: {original_threshold:.8f}',
        'No model retraining; all additional thresholds derived from Sep-Oct development-site validation SCORES only.',
        'Validation budget = number of flagged site-hours per 1,000 validation site-hours (ties may exceed budget).',
        'Threshold sensitivity is descriptive; NO threshold here has been approved for safety decisions.',
        '']
    for name in ('validation', 'known_site_test', 'unseen_site_test'):
        st = stats[name]
        f = table.loc[(table.split == name) & (table.threshold_policy == 'original_validation_F1')].iloc[0]
        positive = scored.loc[(scored.split == name) & (scored[TARGET] == 1), 'score']
        negative = scored.loc[(scored.split == name) & (scored[TARGET] == 0), 'score']
        ep = episodes.loc[episodes.split == name]
        lines += [f'{name.upper()}:',
                  f'  Plants={st["plants"]}, site-hours={st["site_hours"]:,}, positive site-hours={st["positive_site_hours"]:,}',
                  f'  AP={st["average_precision"]:.6f}; ROC-AUC={st["roc_auc"]:.6f}; prevalence={st["prevalence"]:.6f}',
                  f'  Positive scores: median={positive.median():.7f}, p90={positive.quantile(.90):.7f}, max={positive.max():.7f}',
                  f'  Negative scores: median={negative.median():.7f}, p99={negative.quantile(.99):.7f}, max={negative.max():.7f}',
                  f'  At original threshold: flagged={int(f.flagged_site_hours):,}, TP={int(f.tp)}, FP={int(f.fp)}, FN={int(f.fn)}, recall={f.recall:.2%}',
                  f'  Fixed-threshold alert episodes={len(ep):,}, containing a positive labeled site-hour={int(ep.has_positive_labeled_hour.sum()):,}',
                  '  Validation-derived site-hour workload sensitivity (thresholds NOT selected on test):']
        for budget in BUDGETS:
            key = f'validation_budget_{budget:g}_per_1000'
            r = table.loc[(table.split == name) & (table.threshold_policy == key)].iloc[0]
            lines.append(f'    {budget:g}/1k validation budget: threshold={r.threshold_from_validation:.7f}, '
                         f'flags={int(r.flagged_site_hours):,} ({r.flags_per_1000_site_hours:.2f}/1k), '
                         f'TP={int(r.tp)}, FP={int(r.fp)}, recall={r.recall:.1%}, precision={r.precision:.2%}')
        if events is not None:
            e0 = detections.loc[(detections.split == name) & (detections.threshold_policy == 'original_validation_F1')]
            if len(e0):
                e0 = e0.iloc[0]
                lines.append(f'  Reachable warning issuance opportunities={int(e0.reachable_site_warning_issuances):,}; '
                             f'detected at original threshold={int(e0.detected_site_warning_issuances):,}')
        lines.append('')
    lines += [
        'INTERPRETATION LIMITS:',
        '- Test positive/negative labels are used only to EVALUATE strategies chosen from development validation.',
        '- Lower-threshold comparisons are a sensitivity exercise, not a tuning or implementation recommendation.',
        '- Counts here are correlated site-hours and plant-warning issuances, NOT independent storms.',
        '- Episode counting is a heuristic; matching a positive label is not verified useful lead time.',
        '- Geographic holdout uses 5-degree cells/shared-station grouping; cross-boundary weather correlation remains.',
        '- The feature timestamps assumed a 60-minute observation reporting delay, not actual archived receipt times.',
        '- Archived warnings can be incomplete, and target is NWS warning issuance, NOT lightning or all work hazards.',
        '- Never use model predictions to override authoritative alerts, field observations, site policy or stop-work rules.',
    ]
    summary = '\n'.join(lines) + '\n'
    (a.out/'summary.txt').write_text(summary, encoding='utf-8')
    print('\n' + summary)
    print('Saved diagnostics:', a.out)

if __name__ == '__main__':
    main()
