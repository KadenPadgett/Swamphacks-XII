"""Recurring refreshes owned by the backend lifespan, independent of dashboard traffic."""

import asyncio
import logging
import os
import time

import database
import lightning
import risk
import strike_feed
import briefings
import hefs
from refresh_locks import RefreshBusy, lease
from risk_sources import RiskSources, http_client, parse_time, utcnow

logger = logging.getLogger(__name__)


def intervals():
    return {
        "full": max(10, float(os.environ.get("PLANIT_REFRESH_SECONDS", "300"))),
        "lightning": max(5, float(os.environ.get("PLANIT_LIGHTNING_SECONDS", "30"))),
        "strikes": max(5, float(os.environ.get("PLANIT_STRIKE_SECONDS", "15"))),
        "briefings": 30,
        "hefs": max(900, float(os.environ.get("PLANIT_HEFS_SECONDS", "900"))),
    }


def enabled():
    return os.environ.get("PLANIT_BACKGROUND_REFRESH", "1").lower() not in ("0", "false", "no")


async def run(kind="full", sites=None, trigger="manual", scheduled=False, interval=None):
    if kind not in ("full", "lightning", "strikes", "briefings", "hefs"):
        raise ValueError("Unknown refresh job")
    async with lease("job:" + kind):
        latest = database.latest_run(kind)
        interval = intervals()[kind] if interval is None else interval
        if scheduled and latest and latest["finished_at"]:
            delay = interval if latest["status"] == "succeeded" else min(interval, 30)
            if (utcnow() - parse_time(latest["finished_at"])).total_seconds() < delay:
                return {"status": "not_due", "count": 0, "risks": []}
        # Re-read every run: newly created sites require no registration step.
        sites = (hefs.associated_sites() if kind == 'hefs' else database.list_sites()) if sites is None else sites
        run_id = database.start_run(kind, trigger, utcnow().isoformat(), len(sites))
        results, failures = [], []
        tasks = []
        try:
            if kind == "briefings":
                outcome = await briefings.tick()
                status = "partial" if outcome["failures"] else "succeeded"
                database.finish_run(run_id, utcnow().isoformat(), status, outcome["failures"])
                return {"run_id": run_id, "status": status, **outcome}
            async with http_client() as client:
                sources = RiskSources(client)
                semaphore = asyncio.Semaphore(4)

                async def refresh(site):
                    async with semaphore:
                        try:
                            if kind == "hefs":
                                result = await hefs.refresh_site(site, client)
                            elif kind == "strikes":
                                result = await strike_feed.refresh_site(site, client)
                                result["sources"] = {"measured_lightning": {
                                    "status": result["availability"], "error": result.get("reason"),
                                    "retrieved_at": result.get("last_success_at")}}
                            else:
                                result = (await risk.refresh_site(site, sources) if kind == "full"
                                          else await lightning.refresh_site(site, sources))
                            errors = [f"{name}: {feed.get('error', 'unavailable')}"
                                      for name, feed in result["sources"].items()
                                      if feed["status"] != "available"]
                            if kind == 'hefs' and result['availability'] not in ('available', 'partial'):
                                errors.append('HEFS: ' + str(result.get('reason') or result['availability']))
                            model = result.get("ml_assessment", {})
                            if (model.get("reason") == "model_request_failed_or_invalid_output"
                                    or model.get("latest_attempt", {}).get("reason") == "model_request_failed_or_invalid_output"):
                                errors.append("ML inference failed")
                            error = "; ".join(errors) if errors else None
                            database.record_site_refresh(kind, site["id"], utcnow().isoformat(), error)
                            results.append(result)
                            if error:
                                failures.append({"site_id": site["id"], "error": error})
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            message = f"{type(error).__name__}: {error}"
                            database.record_site_refresh(kind, site["id"], utcnow().isoformat(), message)
                            failures.append({"site_id": site["id"], "error": message})
                            logger.exception("%s refresh failed for site %s", kind, site["id"])

                tasks = [asyncio.create_task(refresh(site)) for site in sites]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            status = "partial" if failures and results else "failed" if failures else "succeeded"
            database.finish_run(run_id, utcnow().isoformat(), status, failures)
            logger.info("%s refresh %s: %d/%d saved, %d failures", kind, run_id, len(results), len(sites), len(failures))
            results.sort(key=lambda item: item["site"]["id"] if kind == "full" else item["site_id"])
            return {"run_id": run_id, "status": status, "count": len(results),
                    "site_count": len(sites), "failures": failures, "risks": results}
        except BaseException as error:
            database.finish_run(run_id, utcnow().isoformat(),
                                "interrupted" if isinstance(error, asyncio.CancelledError) else "failed",
                                [*failures, {"error": type(error).__name__}])
            raise


class RefreshScheduler:
    def __init__(self, periods=None, poll_seconds=5):
        self.periods = intervals() if periods is None else periods
        self.poll_seconds = poll_seconds
        self.tasks = []

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self.loop(kind), name=f"refresh-{kind}") for kind in self.periods]

    async def loop(self, kind):
        while True:
            try:
                await run(kind, trigger="background", scheduled=True, interval=self.periods[kind])
            except RefreshBusy:
                pass  # Another worker/API/CLI owns this run.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s scheduler run failed; will retry", kind)
            await asyncio.sleep(self.poll_seconds)

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []


def status():
    result = database.refresh_status()
    for item in result["leases"]:
        item["active"] = item["expires_at"] > time.time()
    return {"enabled": enabled(), "interval_seconds": intervals(), **result}
