"""PlanIT research-only geographic + chronological holdout validation.

Run: py -3.13 train_dev300_geographic_holdout.py
No weather downloads. Uses saved 300-site CSV.gz, plant coordinates, station IDs.

Split plants WITHOUT USING LABELS into ~70% development and ~30% geographic
holdout, keeping plants in the same 5-degree lat/lon cell and plants sharing a
station together. Fit on development plants Jan-Aug; choose an F1 threshold
on development plants Sep-Oct; evaluate Nov-Dec for BOTH development and
never-seen geographic-holdout plants. A 6-hour prediction-window embargo is
applied at each date boundary. No geographically held-out data is used for
model fitting, early stopping, imputation, or threshold selection.

WARNING: Gridding does not guarantee spatial independence across cell edges;
shared regional storms and spatial autocorrelation remain possible. Single
2025 cold-season test may contain very few distinct warnings. Research only.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DEV = ROOT / 'data/processed/development_300_2025'
DATA = DEV / 'dev300_hourly_training_2025.csv.gz'
PLANTS = DEV / 'dev300_plants.csv'
STATIONS = DEV / 'dev300_nearest_station.csv'
OUT = DEV / 'model_geographic_holdout'
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
PERIODS = {
    'train': ('2025-01-01T00:00:00Z', '2025-08-31T18:00:00Z'),
    'validation': ('2025-09-01T00:00:00Z', '2025-10-31T18:00:00Z'),
    'test': ('2025-11-01T00:00:00Z', '2025-12-31T18:00:00Z'),
}


class DSU:
    def __init__(self, nodes):
        self.parent = {x: x for x in nodes}
    def root(self, x):
        p = self.parent[x]
        if p != x:
            self.parent[x] = self.root(p)
        return self.parent[x]
    def merge(self, a, b):
        x, y = self.root(a), self.root(b)
        if x != y:
            self.parent[max(x, y)] = min(x, y)


def geographic_assignment(grid_degrees, fraction, seed):
    p = pd.read_csv(PLANTS, dtype={'plant_code': 'string'})
    m = pd.read_csv(STATIONS, dtype={'plant_code': 'string', 'station_network':'string', 'station_id':'string'})
    if p.plant_code.duplicated().any() or m.plant_code.duplicated().any():
        raise ValueError('Duplicate plant code in fixed selection or station mapping')
    # The selected-plants file originates from the geographic audit and can
    # already contain nearest-station metadata. Avoid pandas _x/_y suffixes;
    # the fixed dev300_nearest_station.csv is authoritative for station IDs.
    for col in ('station_network', 'station_id'):
        if col in p.columns:
            check = p[['plant_code', col]].merge(
                m[['plant_code', col]], on='plant_code', how='left',
                validate='one_to_one', suffixes=('_plants', '_mapping'))
            a = check[f'{col}_plants'].astype('string').str.strip()
            b = check[f'{col}_mapping'].astype('string').str.strip()
            mismatch = a.notna() & b.notna() & a.ne(b)
            if mismatch.any():
                raise ValueError(f'Conflicting {col} for plant(s): '
                                 f'{check.loc[mismatch, "plant_code"].head(5).tolist()}')
    x = p.drop(columns=['station_network', 'station_id'], errors='ignore').merge(
        m[['plant_code', 'station_network', 'station_id']], on='plant_code',
        validate='one_to_one', how='left')
    if x[['station_network', 'station_id']].isna().any().any():
        raise ValueError('Missing assigned station')
    if not {'plant_lat', 'plant_lon'}.issubset(x.columns):
        raise ValueError(f'Missing plant_lat/plant_lon from {PLANTS}; available: {list(x.columns)}')
    for c in ['plant_lat', 'plant_lon']:
        x[c] = pd.to_numeric(x[c], errors='raise')
    if x[['plant_lat', 'plant_lon']].isna().any().any():
        raise ValueError('Missing coordinates')
    x['grid_cell'] = (np.floor(x.plant_lat / grid_degrees).astype(int).astype(str) + ':' +
                      np.floor(x.plant_lon / grid_degrees).astype(int).astype(str))
    x['station_key'] = x.station_network.astype(str) + '/' + x.station_id.astype(str)
    ids = sorted(x.plant_code.astype(str).tolist())
    union = DSU(ids)
    for col in ('grid_cell', 'station_key'):
        for _, subset in x.groupby(col):
            codes = sorted(subset.plant_code.astype(str))
            for other in codes[1:]:
                union.merge(codes[0], other)
    x['group_id'] = [union.root(i) for i in x.plant_code.astype(str)]
    groups = x.groupby('group_id', sort=True).size()
    rng = np.random.default_rng(seed)
    shuffled = list(groups.index[rng.permutation(len(groups))])
    goal = len(x) * fraction
    chosen = set()
    n = 0
    # All assignments are determined by coordinates/station identities and seed,
    # never by warning events or their prevalence.
    for group in shuffled:
        size = int(groups.loc[group])
        if n < goal and abs((n + size) - goal) <= abs(n - goal) or (n == 0 and size < len(x)):
            chosen.add(group)
            n += size
    if n == 0 or n == len(x):
        raise ValueError('Cannot form two geographic sets; adjust grid size/seed')
    x['geographic_partition'] = np.where(x.group_id.isin(chosen), 'unseen_geographic_test', 'development')
    x['plant_code'] = x.plant_code.astype(str)
    if set(x.loc[x.geographic_partition == 'development', 'station_key']) & set(x.loc[x.geographic_partition != 'development', 'station_key']):
        raise AssertionError('Shared stations across geographic partitions')
    if set(x.loc[x.geographic_partition == 'development', 'grid_cell']) & set(x.loc[x.geographic_partition != 'development', 'grid_cell']):
        raise AssertionError('Shared grid cells across geographic partitions')
    return x


def scores(y, proba, threshold=None):
    from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss
    y = np.asarray(y, dtype=int)
    p = np.asarray(proba, dtype=float)
    row = {'rows': int(y.size), 'positive_site_hours': int(y.sum()),
           'positive_prevalence': float(y.mean()) if y.size else None,
           'mean_predicted_probability': float(p.mean()) if p.size else None}
    if not y.size:
        row['warning'] = 'No rows in partition'
        return row
    row['brier'] = float(brier_score_loss(y, p))
    if len(np.unique(y)) > 1:
        row['average_precision'] = float(average_precision_score(y, p))
        row['roc_auc'] = float(roc_auc_score(y, p))
        row['no_skill_average_precision'] = float(y.mean())
    else:
        row['warning'] = 'Only one label class: AP/ROC-AUC omitted'
    if threshold is not None:
        pred = p >= threshold
        tp = int((pred & (y == 1)).sum())
        fp = int((pred & (y == 0)).sum())
        fn = int((~pred & (y == 1)).sum())
        row['at_validation_threshold'] = {
            'tp_site_hours': tp, 'fp_site_hours': fp, 'fn_site_hours': fn,
            'precision': tp / (tp+fp) if tp+fp else 0.,
            'recall': tp / (tp+fn) if tp+fn else 0.,
            'flags_per_1000_site_hours': 1000 * (tp+fp)/y.size,
        }
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=DATA)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--grid-degrees', type=float, default=5.)
    parser.add_argument('--heldout-fraction', type=float, default=.30)
    parser.add_argument('--train-keep', type=float, default=.35)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--max-iter', type=int, default=120)
    args = parser.parse_args()
    if not (0 < args.heldout_fraction < 1 and 0 < args.train_keep <= 1 and args.grid_degrees > 0):
        parser.error('Invalid fraction/grid setting')
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import precision_recall_curve
    import joblib
    x = geographic_assignment(args.grid_degrees, args.heldout_fraction, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    assignment_path = args.out / 'geographic_site_assignment.csv'
    x.sort_values('plant_code').to_csv(assignment_path, index=False)
    known = set(x.loc[x.geographic_partition == 'development', 'plant_code'])
    unseen = set(x.loc[x.geographic_partition == 'unseen_geographic_test', 'plant_code'])
    print(f'Geographic groups: {x.group_id.nunique()}; development plants: {len(known)}; held-out plants: {len(unseen)}')
    print('Geographic assignment saved:', assignment_path)
    print('All plants sharing a station or 5-degree geographic grid cell stay together.')
    names = ['train', 'validation', 'known_site_test', 'unseen_site_test']
    pieces = {name: [] for name in names}
    needed = list(dict.fromkeys(['plant_code', TIME, TARGET] + FEATURES))
    rng = np.random.default_rng(args.seed)
    total = 0
    for chunk in pd.read_csv(args.input, usecols=needed, dtype={'plant_code':'string'}, chunksize=100_000, low_memory=False):
        total += len(chunk)
        times = pd.to_datetime(chunk[TIME], utc=True, errors='coerce')
        if times.isna().any():
            raise ValueError('Unparseable prediction timestamps')
        group = chunk.plant_code.astype(str)
        bad = ~(group.isin(known) | group.isin(unseen))
        if bad.any():
            raise ValueError(f'Dataset has plant IDs not in fixed selection: {group[bad].head(5).tolist()}')
        train_site = group.isin(known)
        ranges = {
            'train': train_site & times.ge(PERIODS['train'][0]) & times.lt(PERIODS['train'][1]),
            'validation': train_site & times.ge(PERIODS['validation'][0]) & times.lt(PERIODS['validation'][1]),
            'known_site_test': train_site & times.ge(PERIODS['test'][0]) & times.lt(PERIODS['test'][1]),
            'unseen_site_test': (~train_site) & times.ge(PERIODS['test'][0]) & times.lt(PERIODS['test'][1]),
        }
        for name, mask in ranges.items():
            part = chunk.loc[mask, needed].copy()
            if name == 'train' and args.train_keep < 1 and len(part):
                part = part.loc[rng.random(len(part)) < args.train_keep]
            if len(part):
                pieces[name].append(part)
    data = {}
    for name in names:
        if not pieces[name]:
            raise ValueError(f'Empty partition: {name}')
        df = pd.concat(pieces[name], ignore_index=True)
        df[TARGET] = pd.to_numeric(df[TARGET], errors='raise').astype('int8')
        for feature in FEATURES:
            df[feature] = pd.to_numeric(df[feature], errors='coerce').astype('float32')
        data[name] = df
        print(f'{name}: {len(df):,} rows; {int(df[TARGET].sum()):,} positive site-hours; {df.plant_code.nunique()} plants')
    if data['train'][TARGET].nunique() != 2:
        raise ValueError('Train data contains only one class')
    if data['validation'][TARGET].nunique() != 2:
        raise ValueError('Validation lacks positives or negatives; cannot select threshold')
    model = HistGradientBoostingClassifier(
        max_iter=args.max_iter, learning_rate=.05, max_leaf_nodes=15,
        min_samples_leaf=80, l2_regularization=10,
        early_stopping=False, random_state=args.seed,
    )
    print('Training weather classifier on development plants, Jan-Aug 2025...')
    model.fit(data['train'][FEATURES], data['train'][TARGET])
    vprob = model.predict_proba(data['validation'][FEATURES])[:, 1]
    vp = data['validation'][TARGET].to_numpy()
    prec, rec, thresholds = precision_recall_curve(vp, vprob)
    f1 = 2 * prec[:-1] * rec[:-1] / np.maximum(prec[:-1] + rec[:-1], 1e-12)
    idx = int(np.argmax(f1))
    threshold = float(thresholds[idx])
    print(f'Validation-selected threshold: {threshold:.8f}')
    result = {
        'research_only': True,
        'target': 'new severe thunderstorm warning covering site within next 6h',
        'seed': args.seed,
        'grid_degrees': args.grid_degrees,
        'requested_geographic_holdout_fraction': args.heldout_fraction,
        'development_plants': len(known), 'unseen_geographic_test_plants': len(unseen),
        'geographic_groups': int(x.group_id.nunique()),
        'train_keep': args.train_keep,
        'features': FEATURES,
        'threshold_selected_from_development_validation_only': threshold,
        'splits': {},
        'notes': [
            'Geographic partitions are outcome-blind and keep shared stations and 5-degree grid cells together.',
            'Validation Sep-Oct on development plants; both test groups Nov-Dec.',
            'Six-hour chronological embargo is applied at temporal boundaries.',
            'Cross-border/cross-grid storms can still make locations spatially correlated.',
            'Neither station representativeness nor warning archive completeness is established.',
            'No predictions should determine dispatch or crew stop-work decisions.',
        ],
    }
    for name in ('validation', 'known_site_test', 'unseen_site_test'):
        y = data[name][TARGET].to_numpy()
        proba = vprob if name == 'validation' else model.predict_proba(data[name][FEATURES])[:, 1]
        result['splits'][name] = scores(y, proba, threshold)
        print(name, json.dumps(result['splits'][name], indent=2))
    joblib.dump({'model':model, 'features': FEATURES, 'research_only':True}, args.out / 'geographic_hgb.joblib')
    (args.out / 'geographic_metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    summary = [
        'PlanIT geographic holdout · research only',
        f'Groups: {x.group_id.nunique()}, development plants: {len(known)}, unseen plants: {len(unseen)}',
        f'Validation-selected threshold: {threshold:.8f}',
    ]
    for name in ('validation', 'known_site_test', 'unseen_site_test'):
        row = result['splits'][name]
        summary.append(f'{name}: rows={row["rows"]:,}, positive site-hours={row["positive_site_hours"]:,}, '
                       f'prevalence={row["positive_prevalence"]:.6f}, '
                       f'AP={row.get("average_precision", "undefined")}, ROC-AUC={row.get("roc_auc", "undefined")}, '
                       f'recall={row["at_validation_threshold"]["recall"]:.4f}, '
                       f'precision={row["at_validation_threshold"]["precision"]:.4f}')
    summary.append('Caution: same 2025 season, sparse/clustered warnings, and gridded proximity do not establish deployment validity.')
    (args.out / 'geographic_summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
    print('\n' + '\n'.join(summary))
    print('Saved:', args.out)


if __name__ == '__main__':
    main()
