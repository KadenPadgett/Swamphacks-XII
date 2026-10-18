"""Read-only Linux process inspection; never prints arguments or environment."""
import json
from pathlib import Path
import os

matches = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        args = (entry / 'cmdline').read_bytes().split(b'\0')
        if any(b'uvicorn' in arg or b'scripts/serve.py' in arg or b'scripts/refresh_risks.py' in arg for arg in args):
            matches.append({'pid': int(entry.name), 'process': (entry / 'comm').read_text().strip()})
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
print(json.dumps({'backend_processes': matches}))
