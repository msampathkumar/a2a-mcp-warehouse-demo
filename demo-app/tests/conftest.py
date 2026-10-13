import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from warehouse.data_manager import DataManager  # noqa: E402
from warehouse.telemetry import Recorder  # noqa: E402


@pytest.fixture(scope='session')
def recorder(tmp_path_factory):
    return Recorder(tmp_path_factory.mktemp('runs') / 'events.jsonl')


@pytest.fixture(scope='session')
def dm_level5(recorder):
    return DataManager(
        APP_DIR / 'data',
        ['orders', 'stores', 'customers', 'order_items', 'products'],
        recorder,
        row_cap=500,
        source_latency_ms=0,
    )


@pytest.fixture(scope='session')
def dm_level1(recorder):
    return DataManager(
        APP_DIR / 'data', ['orders'], recorder, row_cap=500, source_latency_ms=0
    )
