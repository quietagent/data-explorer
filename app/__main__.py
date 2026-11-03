"""Start Data Explorer:  python -m app [--demo] [--port 8000] [--no-browser]"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from . import config


def _free_port(start):
    """The first free port from `start` on the loopback address."""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((config.HOST, port))
                return port
            except OSError:
                continue
    sys.exit(f'No free port between {start} and {start + 19}.')


def _open_when_ready(url, port):
    for _ in range(100):
        try:
            with socket.create_connection((config.HOST, port), timeout=0.3):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.15)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m app', description='Data Explorer - local, read-only.')
    ap.add_argument('--demo', action='store_true', help='open the demo workspace (sample telecom data)')
    ap.add_argument('--port', type=int, default=config.PORT, help=f'port to use (default {config.PORT})')
    ap.add_argument('--no-browser', action='store_true', help='do not open the browser')
    args = ap.parse_args(argv)
    if sys.version_info < (3, 11):
        sys.exit('Data Explorer needs Python 3.11 or newer.')

    import uvicorn
    from .main import create_app

    app = create_app()
    port = _free_port(args.port)
    url = f'http://{config.HOST}:{port}/'
    if args.demo:
        url += '?ws=' + app.state.workspaces.ensure_demo()
    print(f'\n  {config.APP_NAME} {config.APP_VERSION} is running at {url}')
    print('  Local only: it listens on 127.0.0.1 and sends nothing off this PC.')
    print('  Close this window (or press Ctrl+C) to stop.\n')
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    # host is fixed to the loopback address on purpose - there is no option to change it
    uvicorn.run(app, host=config.HOST, port=port, log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
