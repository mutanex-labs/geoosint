import pytest

from geoosint.config import Settings
from geoosint.mcp.server import Runtime


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(Settings(data_dir=tmp_path))
    yield runtime
    runtime.close()
