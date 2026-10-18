"""Audited restoration of the reviewed SQLite recovery; stop backend writers first.

--restore replaces only a source matching the recovery manifest, preserving backup.
--refresh-missing regenerates unreadable assessments and copies the healthy result
to the configured local runtime path only when that path does not already exist.
Never reads or prints credentials from .env.
"""
import argparse
from contextlib import closing
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import database

MANIFEST = ROOT / '.cache/sites.recovery.20261017T203904Z.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        checks = [row[0] for row in conn.execute('PRAGMA integrity_check')]
        return {'path': str(path), 'integrity_check': checks,
                'site_count': conn.execute('SELECT count(*) FROM sites').fetchone()[0],
                'assessment_count': conn.execute('SELECT count(*) FROM risk_snapshots').fetchone()[0]}


def backup_copy(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            dst.commit()


def configured_path():
    value = None
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            if line.strip().startswith('PLANIT_DATABASE_PATH='):
                value = line.split('=', 1)[1].strip().strip('"').strip("'")
    path = Path(value or 'sites.db')
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(ROOT):
        raise SystemExit('Configured storage is outside this workspace; copy through an approved host operation')
    return path


async def refresh(missing):
    import refresh_jobs
    sites = [database.find_site(site_id) for site_id in missing]
    result = await refresh_jobs.run('full', sites=[site for site in sites if site], trigger='database_recovery')
    return {key: result[key] for key in ('count', 'status', 'site_count', 'failures')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--refresh-missing', action='store_true')
    args = parser.parse_args()
    source = ROOT / 'sites.db'
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    recovered = Path(manifest['recovered'])
    missing = [row['readable_metadata'][0] for row in manifest['tables']['risk_snapshots']['failed_rows']]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    audit = {'time': stamp, 'missing_assessment_ids': missing, 'source': str(source)}
    if args.restore:
        if digest(source) != manifest['untouched_source_sha256']:
            raise SystemExit('Source changed since recovery: refusing to overwrite newer data')
        if inspect(recovered)['integrity_check'] != ['ok']:
            raise SystemExit('Recovery candidate failed integrity check')
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise SystemExit('Nonempty SQLite journal/WAL found; stop writers and reconcile it first')
        original = ROOT / '.cache' / ('sites.pre-repair.' + stamp + '.db')
        shutil.copy2(source, original)
        staged = ROOT / ('.sites-repair-' + stamp + '.db')
        backup_copy(recovered, staged)
        database.DATABASE_PATH = staged
        database.initialize_database()
        if inspect(staged)['integrity_check'] != ['ok']:
            raise SystemExit('Migrated recovery failed integrity check; source unchanged')
        if digest(source) != manifest['untouched_source_sha256']:
            raise SystemExit('Source changed during repair; source unchanged')
        os.replace(staged, source)
        audit['corrupt_backup'] = str(original)
        audit['recovery_source'] = str(recovered)
    database.DATABASE_PATH = source
    health = inspect(source)
    if health['integrity_check'] != ['ok']:
        print(json.dumps(health))
        return 1
    if args.refresh_missing:
        database.initialize_database()
        audit['refresh'] = asyncio.run(refresh(missing))
        target = configured_path()
        if target != source:
            if target.exists():
                raise SystemExit('Configured database exists; refusing to overwrite it')
            target.parent.mkdir(parents=True, exist_ok=True)
            backup_copy(source, target)
            audit['configured_database'] = inspect(target)
    audit['source_database'] = inspect(source)
    audit_path = ROOT / '.cache' / ('database-repair.' + stamp + '.json')
    audit_path.write_text(json.dumps(audit, indent=2), encoding='utf-8')
    print(json.dumps({'audit': str(audit_path), **audit}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
