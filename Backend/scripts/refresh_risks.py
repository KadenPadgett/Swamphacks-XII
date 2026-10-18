"""Refresh every monitored site, including frontend-created custom sites."""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
import refresh_jobs
from refresh_locks import RefreshBusy

async def main():
    database.initialize_database()
    try:
        result = await refresh_jobs.run("full", trigger="cli")
        print(f"Refreshed {result['count']} monitored sites; status: {result['status']}.")
    except RefreshBusy:
        print("A full refresh is already running; skipped this duplicate request.")

if __name__ == "__main__":
    asyncio.run(main())
