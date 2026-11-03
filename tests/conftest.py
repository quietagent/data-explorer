"""Shared fixtures. Every test that needs the app gets its own empty data folder."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def local_client(app, **kwargs):
    """A test client that looks like a browser on this PC talking to the app."""
    kwargs.setdefault('base_url', 'http://127.0.0.1:8000')
    kwargs.setdefault('client', ('127.0.0.1', 50000))
    return TestClient(app, **kwargs)


@pytest.fixture(scope='session')
def demo_dir():
    """data/demo with the SQLite database, the .sql file and the CSV exports."""
    return demo.build_demo()


@pytest.fixture(scope='session')
def demo_sql(demo_dir):
    return demo_dir / 'telecom.sql'


@pytest.fixture(scope='session')
def demo_db(demo_dir):
    return demo_dir / 'telecom.db'


@pytest.fixture(scope='session')
def demo_csv(demo_dir):
    return demo_dir / 'csv'


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / 'data')


@pytest.fixture
def client(app):
    with local_client(app) as c:
        yield c
