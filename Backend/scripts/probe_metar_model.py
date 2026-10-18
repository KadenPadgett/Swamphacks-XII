"""Check live IEM observations and the frozen model without touching the site DB."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
import metar
import metar_model
from risk_sources import utcnow


async def probe(latitude, longitude):
    with TemporaryDirectory() as folder:
        database.DATABASE_PATH = Path(folder) / 'sites.db'
        database.initialize_database()
        site = {'id': 999, 'latitude': latitude, 'longitude': longitude}
        now = utcnow()
        async with httpx.AsyncClient() as client:
            observation = await metar.assess(site, client, now)
        model = await metar_model.predict({'site': site, 'generated_at': now.isoformat()}, observation)
        return {
            'metar_availability': observation['availability'],
            'metar_reason': observation.get('reason'),
            'station_id': (observation.get('station') or {}).get('station_id'),
            'current_observed_at': observation.get('current_observed_at'),
            'prior_observed_at': observation.get('prior_observed_at'),
            'model_availability': model['availability'],
            'model_reason': model.get('reason'),
            'model_version': model.get('model_version'),
            'target': model['target'],
            'probability': model.get('probability'),
            'score': model.get('score'),
            'outcome_end': model.get('outcome_end'),
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--latitude', type=float, default=25.75)
    parser.add_argument('--longitude', type=float, default=-80.70)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.latitude, args.longitude)), indent=2))
