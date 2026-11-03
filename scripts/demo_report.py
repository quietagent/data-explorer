"""Save one report of the demo workspace, HTML and Excel, to look at by hand.

    python -m scripts.demo_report [folder]          default: data\\checks (git-ignored)

The demo workspace is built in a temporary data folder (the app's own data\\explorer.db is not
touched), every table is profiled, every relationship is checked, and the two files the Report tab
would download are written to the folder: demo-report.html and demo-report.xlsx.
"""

from __future__ import annotations

import sys
import tempfile
import time
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter('ignore')                 # the test client's note about httpx (see pytest.ini)
    from fastapi.testclient import TestClient

from app import config
from app.main import create_app


def wait(client, job, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.2)
    raise SystemExit('the job did not finish')


def save(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
        app = create_app(Path(data))
        with TestClient(app, base_url=f'http://{config.HOST}:{config.PORT}', client=(config.HOST, 50000)) as client:
            ws = app.state.workspaces.ensure_demo()
            profiled = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
            checked = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
            written = []
            for kind in ('html', 'xlsx'):
                response = client.get(f'/api/workspaces/{ws}/report?format={kind}')
                if response.status_code != 200:
                    raise SystemExit(f'report?format={kind}: {response.status_code} {response.text[:200]}')
                path = folder / f'demo-report.{kind}'
                path.write_bytes(response.content)
                written.append(path)
        print(f'profiled {profiled["done"]} tables, checked {checked["done"]} relationships')
    for path in written:
        print(f'{path}  {path.stat().st_size:,} bytes')
    return written


if __name__ == '__main__':
    save(sys.argv[1] if len(sys.argv) > 1 else config.BASE_DIR / 'data' / 'checks')
