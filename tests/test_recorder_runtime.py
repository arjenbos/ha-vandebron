"""Exercise the actual HA recorder and SQL statistics import, without production APIs."""

import asyncio
from datetime import UTC, datetime

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers.recorder import DATA_RECORDER, RecorderData
from homeassistant.loader import async_setup as setup_loader
from homeassistant.setup import async_setup_component

from custom_components.vandebron.models import AMSTERDAM, Source
from custom_components.vandebron.statistics import async_import_usage


async def test_real_recorder_import_is_idempotent(hass, measurements, tmp_path):
    hass.config_entries = ConfigEntries(hass, {})
    setup_loader(hass)
    hass.data[DATA_RECORDER] = RecorderData()
    assert await async_setup_component(
        hass, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path}/test.db"}}
    )
    await hass.async_start()
    recorder = get_instance(hass)
    async with asyncio.timeout(20):
        await recorder.async_block_till_done()
        year = datetime.now(UTC).astimezone(AMSTERDAM).date().replace(month=1, day=1)
        source = Source(
            "account_one", "One", "account", blocks={f"usage_year_{year}": measurements}
        )
        stat_id = "vandebron:account_one_electricity_usage"
        for _ in range(2):
            await async_import_usage(hass, {source.key: source})
            await recorder.async_block_till_done()
            latest = await recorder.async_add_executor_job(
                get_last_statistics, hass, 1, stat_id, True, {"sum"}
            )
            assert latest[stat_id][-1]["sum"] == pytest.approx(15.9)
        measurements["electricity"]["intervals"][0]["usage"]["value"] = "5"
        await async_import_usage(hass, {source.key: source})
        await recorder.async_block_till_done()
        latest = await recorder.async_add_executor_job(
            get_last_statistics, hass, 1, stat_id, True, {"sum"}
        )
        assert latest[stat_id][-1]["sum"] == pytest.approx(13.1)
