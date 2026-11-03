"""The localhost guard, and "nothing leaves this PC": no CDN, no remote calls, no docs page."""

import re

import pytest

from app import config
from app.security import (CONTENT_SECURITY_POLICY, check_request, host_name, is_local_client, is_local_host,
                          is_same_local_origin)
from conftest import ROOT, local_client


# ------------------------------------------------------------------ the rules

@pytest.mark.parametrize('value, expected', [
    ('127.0.0.1:8000', '127.0.0.1'), ('localhost', 'localhost'), ('LOCALHOST:8000', 'localhost'),
    ('[::1]:8000', '::1'), ('[::1]', '::1'), ('example.com:80', 'example.com'),
    ('', None), (None, None), ('localhost:8000/path', None), ('user@localhost', None), ('localhost:abc', None),
    ('[::1', None), ('local host', None),
])
def test_host_name(value, expected):
    assert host_name(value) == expected


@pytest.mark.parametrize('host', ['127.0.0.1', '127.0.0.1:8000', 'localhost:8000', 'LocalHost', '[::1]:8000'])
def test_local_hosts_are_accepted(host):
    assert is_local_host(host)


@pytest.mark.parametrize('host', [
    'evil.example', 'evil.example:8000', '127.0.0.1.evil.example', 'localhost.evil.example:8000', 'notlocalhost',
    '192.168.1.20:8000', '10.0.0.5', '0.0.0.0:8000', '127.0.0.2:8000', 'my-pc:8000', '', None, 'localhost@evil.example',
])
def test_other_hosts_are_refused(host):
    assert not is_local_host(host)


def test_origin_must_be_this_very_app():
    assert is_same_local_origin('http://127.0.0.1:8000', '127.0.0.1:8000')
    assert is_same_local_origin('http://localhost:8000', 'localhost:8000')
    assert not is_same_local_origin('http://localhost:3000', 'localhost:8000')        # another local app
    assert not is_same_local_origin('http://127.0.0.1:8000', 'localhost:8000')
    assert not is_same_local_origin('https://evil.example', '127.0.0.1:8000')
    assert not is_same_local_origin('http://127.0.0.1.evil.example:8000', '127.0.0.1:8000')
    assert not is_same_local_origin('null', '127.0.0.1:8000')
    assert not is_same_local_origin('file://', '127.0.0.1:8000')
    assert not is_same_local_origin('', '127.0.0.1:8000')


def test_only_loopback_clients():
    assert is_local_client('127.0.0.1') and is_local_client('::1') and is_local_client('127.0.0.5')
    assert not is_local_client('192.168.1.20') and not is_local_client('testclient') and not is_local_client('')


def test_check_request_combines_the_rules():
    ok = {'host': '127.0.0.1:8000'}
    assert check_request('127.0.0.1', ok, '/api/health') is None
    assert check_request('127.0.0.1', {**ok, 'origin': 'http://127.0.0.1:8000'}, '/api/workspaces') is None
    assert check_request('192.168.1.20', ok, '/') is not None
    assert check_request(None, ok, '/') is not None
    assert check_request('127.0.0.1', {}, '/') is not None                             # no Host header at all
    assert check_request('127.0.0.1', {'host': 'evil.example'}, '/') is not None
    assert check_request('127.0.0.1', {**ok, 'origin': 'https://evil.example'}, '/api/workspaces') is not None
    assert check_request('127.0.0.1', {**ok, 'sec-fetch-site': 'cross-site'}, '/api/workspaces') is not None
    assert check_request('127.0.0.1', {**ok, 'sec-fetch-site': 'cross-site'}, '/') is None   # a link to the page is fine
    assert check_request('127.0.0.1', {**ok, 'sec-fetch-site': 'same-origin'}, '/api/workspaces') is None


# ------------------------------------------------------------ through the app

def test_requests_from_this_pc_are_served(client):
    assert client.get('/api/health').json()['status'] == 'ok'
    assert client.get('/api/health', headers={'host': 'localhost:8000'}).status_code == 200
    assert client.get('/api/health', headers={'host': '[::1]:8000'}).status_code == 200
    assert client.get('/').status_code == 200


@pytest.mark.parametrize('host', ['evil.example', '127.0.0.1.evil.example:8000', 'localhost.evil.example',
                                  '192.168.1.20:8000', 'my-pc:8000'])
def test_a_foreign_host_header_is_refused(client, host):
    for path in ('/api/health', '/api/workspaces', '/', '/static/app.js'):
        response = client.get(path, headers={'host': host})
        assert response.status_code == 403, path
        assert response.json() == {'detail': 'Host header is not localhost.'}


@pytest.mark.parametrize('origin', ['https://evil.example', 'http://evil.example:8000', 'null',
                                    'http://localhost:3000', 'http://127.0.0.1:9999', 'http://localhost:8000'])
def test_a_foreign_origin_is_refused_and_changes_nothing(client, origin):
    before = client.get('/api/workspaces').json()
    response = client.post('/api/workspaces', json={'name': 'from another site'}, headers={'origin': origin})
    assert response.status_code == 403 and response.json() == {'detail': 'Origin is not this app.'}
    assert client.delete('/api/workspaces/anything', headers={'origin': origin}).status_code == 403
    assert client.get('/api/workspaces').json() == before


def test_the_apps_own_origin_is_accepted(client):
    response = client.post('/api/workspaces', json={'name': 'mine'}, headers={'origin': 'http://127.0.0.1:8000'})
    assert response.status_code == 200 and response.json()['name'] == 'mine'


def test_cross_site_fetches_are_refused(client):
    assert client.get('/api/workspaces', headers={'sec-fetch-site': 'cross-site'}).status_code == 403
    assert client.get('/api/workspaces', headers={'sec-fetch-site': 'same-origin'}).status_code == 200


def test_clients_that_are_not_this_pc_are_refused(app):
    with local_client(app, client=('192.168.1.20', 50000)) as remote:
        response = remote.get('/api/health')
        assert response.status_code == 403
        assert response.json() == {'detail': 'Only connections from this PC are accepted.'}
        assert remote.get('/').status_code == 403


def test_refusals_come_before_any_route_runs(app):
    with local_client(app) as c:
        assert c.post('/api/db/test', json={'engine': 'sqlite', 'database': 'x'},
                      headers={'host': 'evil.example'}).status_code == 403
        assert c.get('/api/workspaces/none/export', headers={'host': 'evil.example'}).status_code == 403


# ------------------------------------------------------- nothing leaves the PC

def test_security_headers(client):
    for path in ('/', '/api/health', '/static/app.js'):
        headers = client.get(path).headers
        assert headers['content-security-policy'] == CONTENT_SECURITY_POLICY
        assert headers['x-content-type-options'] == 'nosniff'
        assert headers['x-frame-options'] == 'DENY'
        assert headers['referrer-policy'] == 'no-referrer'
    assert 'access-control-allow-origin' not in client.get('/api/health', headers={'origin': 'http://127.0.0.1:8000'}).headers
    assert client.get('/api/health').headers['cache-control'] == 'no-store'
    assert client.get('/static/app.js').headers['cache-control'] == 'no-cache'
    refused = client.get('/api/health', headers={'host': 'evil.example'}).headers
    assert refused['content-security-policy'] == CONTENT_SECURITY_POLICY


def test_content_security_policy_allows_only_this_app():
    policy = dict(part.strip().split(' ', 1) for part in CONTENT_SECURITY_POLICY.split(';'))
    assert policy['default-src'] == "'self'" and policy['script-src'] == "'self'"
    assert policy['connect-src'] == "'self'" and policy['font-src'] == "'self'"
    assert 'http' not in CONTENT_SECURITY_POLICY and '*' not in CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in CONTENT_SECURITY_POLICY and "'unsafe-inline'" not in policy['script-src']


def test_there_is_no_docs_page(client):
    # FastAPI's interactive docs load their scripts from a CDN, so they are switched off.
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert client.get(path).status_code == 404


def test_the_server_binds_to_loopback_only():
    assert config.HOST == '127.0.0.1'
    source = (ROOT / 'app' / '__main__.py').read_text(encoding='utf-8')
    assert 'uvicorn.run(app, host=config.HOST' in source
    assert '0.0.0.0' not in source and '--host' not in source
    run_bat = (ROOT / 'run.bat').read_text(encoding='utf-8')
    assert '--host' not in run_bat and '0.0.0.0' not in run_bat


def test_front_end_loads_nothing_from_outside():
    static = ROOT / 'static'
    index = (static / 'index.html').read_text(encoding='utf-8')
    for name in ('index.html', 'theme.js', 'app.js', 'profile.js', 'analysis.js', 'styles.css'):
        text = (static / name).read_text(encoding='utf-8')
        assert not re.search(r'https?://|//cdn|@import|\burl\(', text, re.I), name
    sources = re.findall(r'<script[^>]*\ssrc="([^"]+)"', index) + re.findall(r'<link[^>]*\shref="([^"]+)"', index)
    assert sources and all(s.startswith('/static/') for s in sources)
    assert not re.search(r'<script(?![^>]*\ssrc=)', index)                 # no inline scripts
    assert not re.search(r'\son[a-z]+\s*=', index)                         # no inline event handlers
    for source in sources:
        assert (ROOT / source.lstrip('/')).is_file(), source


def test_vendor_libraries_are_bundled_and_make_no_requests():
    vendor = ROOT / 'static' / 'vendor'
    names = {'cytoscape.min.js', 'cytoscape-dagre.min.js', 'cytoscape-fcose.js', 'cose-base.js', 'layout-base.js',
             'chart.umd.min.js'}
    assert names <= {p.name for p in vendor.iterdir()}
    for name in names:
        text = (vendor / name).read_text(encoding='utf-8')
        assert not re.search(r'\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|importScripts)\s*\(', text), name
    assert len(list((vendor / 'licenses').iterdir())) == 6
    # the files are exactly the ones recorded when they were downloaded and verified
    import hashlib
    recorded = dict(re.findall(r'^\| (\S+\.js) \|.*\| ([0-9a-f]{64}) \|$', (vendor / 'VERSIONS.md').read_text(), re.M))
    assert set(recorded) == names
    for name, digest in recorded.items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, name


def test_python_code_makes_no_outbound_calls():
    banned = re.compile(r'^\s*(import|from)\s+(requests|urllib\.request|http\.client|httpx|aiohttp|smtplib|ftplib|telnetlib)\b', re.M)
    for path in (ROOT / 'app').rglob('*.py'):
        assert not banned.search(path.read_text(encoding='utf-8')), path
