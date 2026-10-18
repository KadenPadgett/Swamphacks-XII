"""Persist and run a new site's first assessment without delaying site creation."""

import asyncio
from datetime import timedelta
import logging

import database
import risk
from risk_sources import utcnow

logger = logging.getLogger(__name__)
_tasks = set()


def queue(site_id):
    task = asyncio.create_task(run(site_id), name=f'initial-assessment-{site_id}')
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def run(site_id):
    started_at = utcnow().isoformat()
    if not database.claim_initial_assessment(site_id, started_at):
        return False
    try:
        site = database.find_site(site_id)
        if site is None:
            raise ValueError('Site no longer exists')
        initial = database.initial_assessment(site_id)
        existing = database.load_risk(site_id)
        result = (existing if existing and existing.get('site') == site
                  and existing.get('method_version') == risk.METHOD_VERSION
                  and existing.get('assessment_revision') == risk.ASSESSMENT_REVISION
                  and existing.get('generated_at', '') >= initial['queued_at']
                  else await risk.get_site_risk(site))
        failures = [{'source': name, 'status': feed.get('status'),
                     'reason': feed.get('error') or feed.get('reason') or 'unavailable'}
                    for name, feed in result.get('sources', {}).items()
                    if feed.get('status') != 'available']
        for name in ('metar', 'weather_model'):
            feed = result.get(name, {})
            if feed.get('availability') not in ('available',):
                failures.append({'source': name, 'status': feed.get('availability', 'unavailable'),
                                 'reason': feed.get('reason') or 'unavailable'})
        database.finish_initial_assessment(site_id, started_at, utcnow().isoformat(),
                                           'completed', source_failures=failures)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception('Initial assessment failed for site %s', site_id)
        attempts = (database.initial_assessment(site_id) or {}).get('attempts', 1)
        delay = min(30 * (2 ** min(attempts - 1, 5)), 900)
        now = utcnow()
        database.finish_initial_assessment(site_id, started_at, now.isoformat(), 'failed',
                                           error=f'{type(exc).__name__}: {exc}',
                                           next_retry_at=(now + timedelta(seconds=delay)).isoformat())
        return False


async def retry_loop(poll_seconds=5):
    while True:
        try:
            for site_id in database.due_initial_assessments(utcnow().isoformat()):
                if not any(not task.done() and task.get_name() == f'initial-assessment-{site_id}'
                           for task in _tasks):
                    queue(site_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Initial assessment retry scan failed')
        await asyncio.sleep(poll_seconds)


def queue_retries():
    return asyncio.create_task(retry_loop(), name='initial-assessment-retries')


async def stop():
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
