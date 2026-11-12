import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


@pytest.fixture
def entry():
    value = Mock(spec=ConfigEntry)
    value.options = {}
    value.pref_disable_polling = False
    value.entry_id = "test-vandebron"
    return value


@pytest_asyncio.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop()


@pytest.fixture
def measurements():
    return json.loads((Path(__file__).parent / "fixtures/measurements.json").read_text())
