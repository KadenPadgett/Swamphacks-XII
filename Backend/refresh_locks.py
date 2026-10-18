"""Renewable SQLite leases shared by background, API, and CLI workers."""

import asyncio
from contextlib import asynccontextmanager
import time
from uuid import uuid4

import database


class RefreshBusy(Exception):
    pass


@asynccontextmanager
async def lease(name, wait_seconds=0, ttl=60):
    owner = uuid4().hex
    deadline = time.monotonic() + wait_seconds
    while not database.claim_lease(name, owner, time.time(), ttl):
        if time.monotonic() >= deadline:
            raise RefreshBusy(f"{name} is already running")
        await asyncio.sleep(0.1)
    parent = asyncio.current_task()

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(ttl / 3)
                if not database.renew_lease(name, owner, time.time(), ttl):
                    parent.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A worker that cannot renew must stop, not continue after another wins.
            parent.cancel()

    task = asyncio.create_task(heartbeat())
    try:
        yield owner
    finally:
        task.cancel()
        try:
            # Swallow the heartbeat's cancellation, not cancellation of the owner.
            await asyncio.gather(task, return_exceptions=True)
        finally:
            database.release_lease(name, owner)
