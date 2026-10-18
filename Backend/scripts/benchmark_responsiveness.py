"""Local synthetic contention probe; no provider calls or production data."""
import asyncio
import faulthandler
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
import main
import metar_model
import refresh_jobs


async def main_probe():
    with tempfile.TemporaryDirectory() as directory, patch.object(database, "DATABASE_PATH", Path(directory) / "sites.db"), patch.dict(os.environ, {"PLANIT_BACKGROUND_REFRESH": "0"}):
        database.initialize_database()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            async def sample(label, path, duration=1):
                timings = []
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    started = time.monotonic()
                    response = await client.get(path)
                    response.raise_for_status()
                    timings.append(round((time.monotonic() - started) * 1000, 1))
                    await asyncio.sleep(0.03)
                print(f"{label}: count={len(timings)} max_ms={max(timings):.1f} p50_ms={sorted(timings)[len(timings)//2]:.1f}", flush=True)

            await sample("idle health", "/api/health", 0.3)
            await sample("idle saved", "/api/risks", 0.3)

            refresh = asyncio.create_task(refresh_jobs.run("full", sites=[]))
            await asyncio.sleep(0.02)
            await sample("concurrent refresh health", "/api/health", 0.3)
            await sample("concurrent refresh saved", "/api/risks", 0.3)
            await refresh

            def slow_model(_):
                time.sleep(0.01)
                return {"request_id": "test", "model_version": "test", "probability": 0.5}
            active = 0
            maximum = 0
            lock = threading.Lock()
            def counted_model(request):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                try:
                    return slow_model(request)
                finally:
                    with lock:
                        active -= 1
            with patch.object(metar_model, "request_for", return_value={"request_id": "test", "prediction_time": "2026-01-01T00:00:00+00:00", "outcome_end": "2026-01-01T06:00:00+00:00", "station": "TEST", "input_availability": "available", "input_reason": None, "current_observed_at": None, "prior_observed_at": None}), patch.object(metar_model, "local_prediction", side_effect=counted_model):
                model_tasks = [asyncio.create_task(metar_model.predict({"generated_at": "2026-01-01T00:00:00+00:00"}, {"availability": "available", "features": []})) for _ in range(8)]
                await sample("model health", "/api/health", 0.5)
                await sample("model saved", "/api/risks", 0.5)
                await asyncio.gather(*model_tasks)
            print(f"concurrent_model_calls={maximum}", flush=True)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(5, repeat=False)
    asyncio.run(main_probe())
