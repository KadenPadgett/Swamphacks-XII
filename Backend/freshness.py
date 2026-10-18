"""One time policy for assessments; source age and data completeness stay separate."""
from datetime import timedelta
import os
from risk_sources import parse_time, utcnow


def refresh_seconds():
    return max(10, float(os.environ.get('PLANIT_REFRESH_SECONDS', '300')))


def max_age_seconds():
    # Allow a normal refresh interval plus processing/retry margin. Incomplete
    # coverage does not expire the assessment earlier than its refresh schedule.
    return max(900, 2 * refresh_seconds() + 120)


def timestamp(value):
    try:
        return parse_time(value) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def sources(metadata, now=None):
    now = now or utcnow()
    result = {}
    for name, item in (metadata or {}).items():
        retrieved = timestamp(item.get('retrieved_at'))
        expires = timestamp(item.get('expires_at'))
        # Legacy metadata with no provider TTL must not stay fresh forever.
        expires = expires or (retrieved + timedelta(seconds=max_age_seconds()) if retrieved else None)
        result[name] = {**item,
            'stale': bool(item.get('stale')) or (expires <= now if expires else retrieved is None),
            'age_seconds': max(0, (now-retrieved).total_seconds()) if retrieved else None}
    return result


def assessment(snapshot, now=None):
    now = now or utcnow()
    snapshot = snapshot or {}
    generated = timestamp(snapshot.get('generated_at'))
    lifetime = max_age_seconds()
    expires = generated + timedelta(seconds=lifetime) if generated else None
    stale = generated is None or not generated <= now < expires
    source_states = sources(snapshot.get('sources', {}), now)
    failed = any(source.get('status') == 'unavailable' for source in source_states.values())
    delay = min(60, refresh_seconds()) if failed else refresh_seconds()
    due = generated + timedelta(seconds=delay) if generated else None
    return {'stale': stale, 'reason': 'assessment_missing_or_expired' if stale else None,
            'assessment_generated_at': snapshot.get('generated_at'),
            'weather_observed_at': snapshot.get('current_risk', {}).get('weather_observed_at'),
            'expires_at': expires.isoformat() if expires else None, 'max_age_seconds': lifetime,
            'refresh_after_seconds': delay, 'refresh_due_at': due.isoformat() if due else None,
            'refresh_due': due is None or now >= due or stale,
            'coverage_complete': bool(snapshot.get('overall', {}).get('coverage_complete')),
            'sources': source_states}
