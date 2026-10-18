"""Read-only checks against a running backend; never creates sample production data."""
import json
import os
import httpx

base = os.environ.get('PLANIT_API_URL', 'http://127.0.0.1:8000')
with httpx.Client(base_url=base, timeout=30) as client:
    health = client.get('/api/health')
    health.raise_for_status()
    portfolio = client.get('/api/risks')
    portfolio.raise_for_status()
    sites = portfolio.json()['sites']
    jobs = client.get('/api/refresh/status').json()
    print(json.dumps({'health': health.json(), 'site_count': len(sites),
                      'ranking': portfolio.json()['ranking'],
                      'ready_sites': sum(row['status']=='ready' for row in sites),
                      'sites_with_alerts': sum(row['active_alert_status']['count']>0 for row in sites),
                      'intervals': jobs['interval_seconds'],
                      'recent_runs': [{**{k:r[k] for k in ('id','kind','trigger','status','site_count')},
                                       'failure_count':len(r['failures'])} for r in jobs['recent_runs'][:8]]}, indent=2))
    target = next((row for row in sites if row['active_alert_status']['count']), sites[0])
    sid = target['site_id']
    detail = client.get(f'/api/sites/{sid}/detail').json()
    print(json.dumps({'checked_site': sid, 'ml_availability': detail['ml_predictions']['availability'],
                      'current_ml_score': detail['ml_predictions']['current_score'],
                      'strike_coverage': detail['lightning_countdown']['availability'],
                      'timezone': detail['timezone']['timezone']}, indent=2))
    with client.stream('GET', f'/api/sites/{sid}/events', timeout=5) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith('data: '):
                event = json.loads(line[6:])
                assert event['site_id'] == sid
                print(json.dumps({'http_sse_event_received': event['kind'], 'site_id': sid, 'id': event['id']}))
                break
            if line.startswith(': heartbeat'):
                print('HTTP SSE connected; no events exist yet for this site.')
                break
